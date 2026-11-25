# Mendari: project notes for Claude

Python 3.11+ · uv · pydantic v2 · FastAPI · SQLite (local) or Postgres (Vercel, the one-machine install) · no frontend build step.

## Commands

```bash
uv sync                                   # install (dev group included)
uv run pytest -n 6                        # full suite, about 8,500 tests; warnings are errors
uv run pytest -m core                     # the edit loop, about a minute: manifest loading, decisions, policy and its caps, actions, audit
uv run pytest tests/test_<area>.py        # and the file for what you changed (not with -m core, which filters it)
uv run pytest --lf                        # after a failing full run: only what failed
uv run pytest -m browser                  # dashboard in Chromium, about 420 tests (once: uv run playwright install chromium)
uv run pytest -m slow                     # the structural R4 gate over HL7's 5,309 R4 examples (if cached)
# Postgres backend tests (Mendari's own container; never reuse another project's database).
# "too many clients" there is the container's connection limit, not a bug: rerun that file alone.
docker run -d --name mendari-pg-test -e POSTGRES_PASSWORD=mendari-test \
  -e POSTGRES_DB=mendari_test -p 127.0.0.1:56432:5432 postgres:16-alpine
MENDARI_TEST_DATABASE_URL=postgresql://postgres:mendari-test@127.0.0.1:56432/mendari_test uv run pytest -n 6
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
uv run mypy src tests
uv run python scripts/check_doc_snippets.py  # the API examples (dashboard/snippets) on a local server
uv run mendari demo                       # the bundled demo agent end to end, offline
uv run python scripts/demo_agent_data.py  # regenerate the demo agent's datasets and simulated grades (--check in CI)
uv run python -m tests.support.fixture_agents   # regenerate the fixture agents' datasets and cards (--check; the tests run it)
uv run python -m tests.support.fixture_records  # regenerate the chart-summary fixture's FHIR records (--check; likewise)
uv run python scripts/fhir_r4_schema.py   # regenerate the R4 structure rules from HL7's pinned schema
docker compose up --build                 # HAPI FHIR + engine + worker sandbox, on the demo agent
# The one-machine install (deploy/site), run for real with Docker: builds the image, a few minutes.
MENDARI_SITE_E2E=1 uv run pytest tests/test_site_e2e.py -n0 -p no:cacheprovider
docker run --rm -v "$PWD/deploy/site:/mnt:ro" koalaman/shellcheck:stable /mnt/mendari-site /mnt/jobs.sh
# The Hub (hub/, Next.js) against a seeded local registry, as CI's hub-site job: npm ci, lint, typecheck, build, test.
```

## Rules of the codebase

- **No real PHI, ever.** Data is synthetic: the demo agent's comes from
  `scripts/demo_agent_data.py`, the fixture agents' from `tests/support/` (`fixture_agents.py`,
  `fixture_records.py`).
- **Mendari is a separate project.** Never reuse another project's code, connectors,
  credentials, databases, containers or configuration defaults. Before connecting to a local
  port, check which container owns it (`docker ps`).
- **Agents only.** The product bundles one agent, the demo agent `agents/portal-message-agent`
  (`mendari/portal-message-agent`), and no other. The tests decide with synthetic fixture
  agents (`mendari-test/...`): `tests/fixtures/agents/` (`triage`, `clinical-review`,
  `chart-summary`, served by `tests/conftest.py` where bundled agents are) and the small ones in
  `tests/fixtures/mini-agents/`. The product names none of them: a command or script that needs
  an agent the product does not bundle takes `--agent`, and CI gives it the fixture's folder
  (`--agent tests/fixtures/agents/triage`).
- **One vocabulary.** The governed unit is an **agent**, in everything a person reads or types
  and in the code. Its former name appears in no tracked text file outside `notes/`, `tasks/`,
  `licenses/`, vendored folders, lockfiles and binaries, and the five former reference agents'
  ids in none outside `notes/` and `tasks/`. One rule holds both, `tests/test_vocabulary.py`,
  and it lets through only the words that merely hold the letters (`package`, `packet`,
  `unpack`, `backpack`, `struct.pack`).
- **Nothing from before the first release.** 0.1.0 reads nothing a pre-release Mendari made and
  keeps no old name working (ADR-0026). Each store's schema is the first release's baseline
  (`core/db.py`: `mendari-engine/1`, `mendari-registry/1`), and a database a pre-release made is
  refused, never read or migrated. Every format the release writes is at `/1`.
- **The agent and its service.** The governed unit is the agent: `agent_id`, `agent_version`
  and `agent_digest` in decisions, records and the database (a decision's JSON names it under
  `agent`: `id`, `version`, `digest`). The process that answers for an `agent` variant is its
  **service**: the variant's `service_id`, the `service_version` the service reports, a
  calibration's `service`. `type: agent`, `MENDARI_AGENT_URL`, `MENDARI_AGENT_TOKEN` and the
  agent HTTP contract keep the agent's name.
- **The demo agent's data is derived.** `scripts/demo_agent_data.py` writes its `test.jsonl`,
  `written.jsonl` and `written_grades.jsonl`; edit the script, rerun it and commit the script
  with what it wrote. Its canned agent misses paraphrased urgent messages on purpose: the
  evaluation is meant to catch it.
- **Policy only lowers automation.** Caps (per-label, provider failure, risk level) may turn
  AUTO into HUMAN_REVIEW, never the reverse. Risk-level limits live in `core/policy.py`, not
  in manifests (ADR-0002).
- **The action gate lives in `core/policy.py` and only tightens** (ADR-0023): `EFFECT_GATES`,
  `gate_of`, `disposition`. A manifest (`allow_auto: false`), an agent nobody vouched for and
  a second signal that did not agree on a proposal each make a gate stricter; nothing loosens
  one. Mendari authorizes an action and records it, and runs none.
- **A calibration is tied to the service version it was fitted for** (ADR-0024): when the
  agent's service reports another version, or none, nothing is rescaled and the decision is
  capped. The version cap and the second signal's are `apply_policy`'s step 7, the actions'
  cap its step 8, in that order (the module's docstring; `tests/test_caps_together.py`).
- **A judge's figure is withheld unless validated.** Written output (ADR-0025): an agent's
  manifest declares `outputs` and `rubrics`; clinicians grade blind, a model judge grades
  every text, and the judge's pass share for ungraded texts is reported only where its agreement
  with clinicians clears the floors (on the levels and on pass or fail, not leaning lenient, few
  texts it could not grade). `mendari written eval` and `mendari written round create | assign |
  progress | close | list`; the gate and report are `evals/written.py`, the round store
  `core/written.py`, the judge `providers/judge.py`, the console `dashboard/written.js`. The
  floors live in `core/spec.py` and a manifest may only tighten them. A state takes 2,000
  resamples or more, and no command takes a seed once grades exist. A closed round's report is
  served only while its hash is the one the audit chain holds. The demo's three states are
  staged, and it says so.
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

See README "Project layout". Spec: one page, `docs/spec/agent-manifest.md`, for one
`spec_version`, `"0.4"` (`SPEC_VERSION` in `core/spec.py`): a manifest that says another is
refused. Decisions: `docs/decisions/` (ADR-0021: an agent's connection; ADR-0022: the first
administrator and the operator organization; ADR-0023: actions; ADR-0024: a checked confidence
and a second signal; ADR-0025: a judge is trusted by measured agreement; ADR-0026: one release
baseline, agents only). The one-machine install is `deploy/site/` (its script `mendari-site`,
its settings made by `src/mendari/install.py`); the console's Agents, connection and System
views are `dashboard/agents.js`, `connection.js` and `system.js`, over `server/hub_routes.py`,
`connection_routes.py` and `system_routes.py`. Lessons: `tasks/lessons.md`. Plan and status:
`tasks/todo.md` (the current plan at its top, "the engineering to A+"; the vision plan below
it, whose Definition of shipped and build prompts are in `notes/vision-audit.md`, Phase 4).
Plans, reviews and the build log: `notes/` (outside the docs site and the sdist; `docs/` is
only what readers of the site need).
