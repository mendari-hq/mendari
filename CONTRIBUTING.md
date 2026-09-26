# Contributing

Clinicians, informaticists, researchers and engineers are all welcome. Most contributions will
be one of these:

## Improve a pack

1. Change criteria, rules, policy or data generation.
2. Run `./scripts/build-reference-pack.sh` to regenerate data, retrain, run the pack's
   behaviour tests, publish the benchmark and render the Decision Card.
3. Add a `tests/cases.yaml` case for any behaviour you fixed or introduced, especially safety
   behaviour.
4. Bump the pack version (MAJOR for label changes, MINOR for behaviour changes, PATCH for
   documentation) and add a `CHANGELOG.md` entry.

## Contribute a new pack

Start by copying `packs/patient-inbox-router`. A pack is ready for review when:

- `openhealth validate <dir>` and `openhealth test <dir>` pass;
- the Decision Card states intended use, users, population, out-of-scope uses and known
  failure modes honestly;
- the risk level matches the intended use (see `docs/spec/decision-pack-0.1.md` §7);
- a benchmark report is published, and critical labels (things that must never be automated)
  are declared under `evaluation.critical`;
- all data is synthetic or openly licensed and de-identified.

## Code changes

```bash
uv sync
uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src tests
```

Keep changes small and focused. Architectural changes need an ADR in `docs/decisions/`.
