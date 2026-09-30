# OpenHealth Hub: project notes for Claude

Python 3.11+ · uv · pydantic v2 · FastAPI · SQLite (local) or Postgres (Vercel) · no frontend build step.

## Commands

```bash
uv sync                                   # install (dev group included)
uv run pytest                             # full suite, < 3 s, warnings are errors
uv run pytest -m browser                  # dashboard in Chromium (once: uv run playwright install chromium)
# Postgres backend tests (OpenHealth's own container; never reuse another project's database):
docker run -d --name openhealth-pg-test -e POSTGRES_PASSWORD=openhealth-test \
  -e POSTGRES_DB=openhealth_test -p 127.0.0.1:56432:5432 postgres:16-alpine
OPENHEALTH_TEST_DATABASE_URL=postgresql://postgres:openhealth-test@127.0.0.1:56432/openhealth_test uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src tests
uv run openhealth demo                    # end-to-end smoke test on synthetic data
./scripts/build-reference-pack.sh         # both reference packs: data → train → test → benchmark → card
docker compose up --build                 # HAPI FHIR + engine + worker sandbox
```

## Rules of the codebase

- **No real PHI, ever.** Test data comes from `openhealth.sandbox.synthetic_*`.
- **OpenHealth is a separate project.** Never reuse another project's code, connectors,
  credentials, databases, containers or configuration defaults. Before connecting to a local
  port, check which container owns it (`docker ps`).
- **Pack artifacts are derived.** Editing templates in `synthetic_*.py`, `pack.yaml`, rules,
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
- **Dashboard styles use tokens only** (`src/openhealth/dashboard/style.css`, section 1): no hex
  colours, raw spacing or z-index values in components; `tests/test_dashboard_contrast.py` checks
  that, and every colour pair for WCAG AA in both themes. Design plan: `docs/ui-ux-review.md`;
  grade and roadmap: `docs/design-grade.md`.
- **Dashboard scripts are ES modules** with no build step: `main.js` boots, each view registers
  itself in `views` (`router.js`), shared helpers live in `ui.js`. The server stamps every module
  URL through the import map, so a new module needs no registration anywhere.

## Layout

See README "Project layout". Spec: `docs/spec/decision-pack-0.2.md` (0.1 still loads). Decisions:
`docs/decisions/`. Lessons: `tasks/lessons.md`. Plan and status: `tasks/todo.md`.
