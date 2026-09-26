# OpenHealth Hub: project notes for Claude

Python 3.11+ · uv · pydantic v2 · FastAPI · SQLite (stdlib) · no frontend build step.

## Commands

```bash
uv sync                                   # install (dev group included)
uv run pytest                             # full suite, < 2 s, warnings are errors
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src tests
uv run openhealth demo                    # end-to-end smoke test on synthetic data
./scripts/build-reference-pack.sh         # regenerate data → train → test → benchmark → card
docker compose up --build                 # HAPI FHIR + engine + worker sandbox
```

## Rules of the codebase

- **No real PHI, ever.** Test data comes from `openhealth.sandbox.synthetic_inbox`.
- **Pack artifacts are derived.** Editing templates in `synthetic_inbox.py`, `pack.yaml`, rules,
  or policy changes the pack digest and possibly the data and model. Rerun
  `scripts/build-reference-pack.sh` and commit the regenerated `data/`, `models/`,
  `evals/benchmark.json` and `DECISION_CARD.md` together. CI checks data and model are
  byte-reproducible.
- **Policy only lowers automation.** Caps (per-label, provider failure, risk level) may turn
  AUTO into HUMAN_REVIEW, never the reverse. Risk-level limits live in `core/policy.py`, not
  in packs (ADR-0002).
- **Providers never raise anything but `ProviderError`** for expected failures, with stable
  codes and no remote error bodies or credentials in messages.
- **Reports never contain case text**, only ids.
- Keep `pack.yaml` criteria human-readable: they are both documentation and Jev instructions.

## Layout

See README "Project layout". Spec: `docs/spec/decision-pack-0.1.md`. Decisions:
`docs/decisions/`. Lessons: `tasks/lessons.md`. Plan and status: `tasks/todo.md`.
