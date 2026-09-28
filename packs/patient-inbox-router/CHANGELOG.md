# Changelog: openhealth/patient-inbox-router

## 0.3.0

- Safety: the `possible-emergency` rule now escalates when P(urgent) ≥ 0.35 (spec 0.2
  `min_probability`). Before, `urgent` also had to be the most likely label, so the documented
  0.35 threshold never applied and the effective threshold was 0.5.
- `urgency` reworded so its instructions and criteria describe the same thing: a possible
  emergency happening now. The old instructions also included "same-day" care and "answer
  urgent if in doubt"; the doubt threshold now lives in the policy. The criteria list common
  triage red flags and say that past, already-treated episodes are routine.
- `spec_version: "0.2"`. Routing questions, data and the naive Bayes model are unchanged.

## 0.2.0

- New provider variants, so deployments can compare and choose models:
  - `router`: `llm` (any chat model via the AI Gateway or a local OpenAI-compatible
    server) and `cascade` (naive Bayes, then Jev when naive Bayes is below 95%);
  - `safety_screen`: `llm` and `rules_and_llm` (the keyword rules stay as the offline floor).
- With `router=jev` and `safety_screen=rules_and_jev`, one Jev call answers both questions.
- Provider comparison published in `evals/leaderboard.json` and the Decision Card.
- Questions, labels, policy, rules, data and the naive Bayes model are unchanged.

## 0.1.1

- Safety fix: "I just started to vomit a lot of blood" was screened `routine` and auto-routed.
  The keyword screen only knew exact phrasings such as "vomiting up blood".
- New `rules_and_jev` variant for `safety_screen`: rules and Jev both run and the higher
  P(urgent) wins; recommended for every deployment.
- Red-flag rules extended (GI bleeding, coughing blood, bleeding in pregnancy, head injury with
  vomiting, acute abdomen, sudden worst headache, near-fainting, overdose, stroke and
  anaphylaxis phrasings); word gaps never span sentences.
- New evaluation datasets `red_flags` (tuning) and `red_flags_holdout` (never used for
  tuning). Keyword rules alone: 28/28 and 4/20. Jev: 28/28 and 20/20, no false alarms.

## 0.1.0

- First release.
- Questions: `destination` (5 teams) and `urgency` (urgent / routine).
- Providers: deterministic red-flag screen (`rules`); naive Bayes router trained and
  temperature-calibrated on synthetic data (default); keyword router (`rules`) and Jev variants
  for both slots. The Jev endpoint and model are deployment settings (`JEV_API_URL`,
  `JEV_MODEL`), not part of the pack.
- Policy: stat nurse review for possible emergencies; AUTO at ≥0.90 confidence (≥0.95 for
  non-clinical destinations); abstain below 0.50.
