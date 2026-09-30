# Contributing

Clinicians, informaticists, researchers and engineers are all welcome. By taking part you agree
to the [code of conduct](CODE_OF_CONDUCT.md); how decisions are made is in
[GOVERNANCE.md](GOVERNANCE.md). Report security issues privately ([SECURITY.md](SECURITY.md)),
never in a public issue. Most contributions will be one of these:

## Improve a pack

1. Change criteria, rules, policy or data generation.
2. Run `./scripts/build-reference-pack.sh` to regenerate data, retrain, run the pack's
   behaviour tests, publish the benchmark and render the Decision Card.
3. Add a `tests/cases.yaml` case for any behaviour you fixed or introduced, especially safety
   behaviour.
4. Bump the pack version (MAJOR for label changes, MINOR for behaviour changes, PATCH for
   documentation) and add a `CHANGELOG.md` entry.

## Contribute a new pack

Start with `openhealth new <dir> --id <namespace>/<name>` and the
[pack-authoring guide](docs/guides/authoring-a-pack.md), which needs no programming. A pack is
ready for review when:

- `openhealth validate <dir>` and `openhealth test <dir>` pass;
- the Decision Card states intended use, users, population, out-of-scope uses and known
  failure modes honestly;
- the risk level matches the intended use (see `docs/spec/decision-pack-0.2.md` §7);
- a benchmark report is published, and critical labels (things that must never be automated)
  are declared under `evaluation.critical`;
- all data is synthetic or openly licensed and de-identified: **never real patient data**;
- a clinician other than the author has reviewed the criteria (say who in the PR).

### What CI does with a pack PR

The `packs` workflow takes every pack your PR changes and:
- validates it, runs its behaviour tests, and evaluates it on its benchmark dataset;
- renders its Decision Card (an artifact of the run);
- compares its metrics with the benchmark on `main`, in the job summary.

It fails if a critical label is found less often, if a critical case is automated, or if the
primary accuracy or the false-automation rate gets worse by more than the pack's tolerance.
A pack that gets worse on purpose (say, a safer policy that automates less) explains why in the
PR and updates its benchmark.

## Code changes

```bash
uv sync
uv run pytest && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src tests
uv run pytest -m browser    # if you touched the dashboard (once: uv run playwright install chromium)
```

- Keep changes small and focused: one PR does one thing. Write conventional commit messages
  (`feat(…)`, `fix(…)`, `docs:`).
- Add or change a test with every behaviour change.
- Architectural changes need an ADR in `docs/decisions/`.
- Dashboard styles use the design tokens only, and dashboard scripts are ES modules
  (`CLAUDE.md` lists the house rules).
- CI must be green before review. It covers tests on three Pythons, the browser suite, FHIR
  conformance, the sandbox end to end, security scans, the docs build and more.

## Licence

Contributions are accepted under the project's licence, Apache-2.0: whatever you contribute, you
license on the same terms. There is no separate agreement to sign.
