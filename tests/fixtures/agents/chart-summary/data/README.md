# Data: mendari-test/chart-summary

Synthetic test fixture data. Not clinically reviewed. Not for patient care.

`examples.jsonl` (12 cases) and `test.jsonl` (120 cases) are written by
`tests/support/fixture_records.py` (`uv run python -m tests.support.fixture_records`; `--check`
holds the committed files to it). Each line is one synthetic patient's chart, as the agent's FHIR
mapping reads it from the record that module builds for the case, and its labels, which come from
the generator's own facts, never from a provider. Edit the generator, not these files.
