# Mendari: project notes for Claude

Python 3.11+ · uv · pydantic v2 · FastAPI · SQLite (local) or Postgres (Vercel) · no frontend build step.

## Commands

```bash
uv sync                                   # install (dev group included)
uv run pytest                             # full suite, about 6 min (3,500 tests), warnings are errors
uv run pytest -m core                     # the edit loop, about 20 s: manifest loading, decisions, policy, audit
uv run pytest tests/test_<area>.py        # and the file for what you changed (not with -m core, which filters it)
uv run pytest --lf                        # after a failing full run: only what failed
uv run pytest -m browser                  # dashboard in Chromium (once: uv run playwright install chromium)
uv run pytest -m slow                     # the structural R4 gate over HL7's 5,309 R4 examples (if cached)
# Postgres backend tests (Mendari's own container; never reuse another project's database):
docker run -d --name mendari-pg-test -e POSTGRES_PASSWORD=mendari-test \
  -e POSTGRES_DB=mendari_test -p 127.0.0.1:56432:5432 postgres:16-alpine
MENDARI_TEST_DATABASE_URL=postgresql://postgres:mendari-test@127.0.0.1:56432/mendari_test uv run pytest
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src tests
uv run python scripts/check_doc_snippets.py  # the API examples (dashboard/snippets) on a local server
uv run mendari demo                       # end-to-end smoke test on synthetic data
uv run mendari demo                       # the bundled demo agent end to end, offline
uv run python scripts/demo_agent_data.py  # regenerate the demo agent's test set (--check in CI)
uv run python scripts/fhir_r4_schema.py   # regenerate the R4 structure rules from HL7's pinned schema
docker compose up --build                 # HAPI FHIR + engine + worker sandbox
```

## Rules of the codebase

- **No real PHI, ever.** Test data comes from `mendari.sandbox.synthetic_*`.
- **Mendari is a separate project.** Never reuse another project's code, connectors,
  credentials, databases, containers or configuration defaults. Before connecting to a local
  port, check which container owns it (`docker ps`).
- **Agents only.** The product bundles one agent, `agents/portal-message-agent` (its manifest
  is still named `pack.yaml`; routes are still `/api/packs/...`). The five former Decision Packs
  are test fixtures (`tests/fixtures/reference-packs`, served by `tests/conftest.py`) and live on
  the branch `archive/decision-packs`; never add them back to the product. UI and docs say
  "agent", never "pack".
- **The demo agent's data is derived.** `scripts/demo_agent_data.py` writes its `test.jsonl`;
  edit the script, rerun it and commit both. Its canned agent misses paraphrased urgent
  messages on purpose: the evaluation is meant to catch it.
- **Policy only lowers automation.** Caps (per-label, provider failure, risk level) may turn
  AUTO into HUMAN_REVIEW, never the reverse. Risk-level limits live in `core/policy.py`, not
  in manifests (ADR-0002).
- **Providers never raise anything but `ProviderError`** for expected failures, with stable
  codes and no remote error bodies or credentials in messages.
- **Reports never contain case text**, only ids.
- Keep manifest criteria human-readable: they are both documentation and the agent's instructions.
- **Dashboard styles use tokens only** (`src/mendari/dashboard/style.css`, section 1): no hex
  colours, raw spacing or z-index values in components; `tests/test_dashboard_contrast.py` checks
  that, and every colour pair for WCAG AA in both themes. Design plan: `notes/ui-ux-review.md`;
  grade and roadmap: `notes/design-grade.md`.
- **Dashboard scripts are ES modules** with no build step: `main.js` boots, each view registers
  itself in `views` (`router.js`), shared helpers live in `ui.js`; `docs.js` runs the `/docs`
  landing (`docs.html`, filled by `server/landing.py`). The server stamps every module a page's
  scripts import through that page's import map, so a new module needs no registration anywhere.

## Layout

See README "Project layout". Spec: `docs/spec/decision-pack-0.2.md` (0.1 still loads). Decisions:
`docs/decisions/`. Lessons: `tasks/lessons.md`. Plan and status: `tasks/todo.md` (the vision plan at its top; its Definition of shipped and
build prompts are in `notes/vision-audit.md`, Phase 4). Plans, reviews
and the build log: `notes/` (outside the docs site and the sdist; `docs/` is only what readers
of the site need).
