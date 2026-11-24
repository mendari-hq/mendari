# Mendari: project notes for Claude

Python 3.11+ · uv · pydantic v2 · FastAPI · SQLite (local) or Postgres (Vercel, the one-machine install) · no frontend build step.

## Commands

```bash
uv sync                                   # install (dev group included)
uv run pytest -n 6                        # full suite, 10,400 tests: about 15 min on six workers; warnings are errors
uv run pytest -m core                     # the edit loop, about 20 s: manifest loading, decisions, policy and its caps, actions, audit
uv run pytest tests/test_<area>.py        # and the file for what you changed (not with -m core, which filters it)
uv run pytest --lf                        # after a failing full run: only what failed
uv run pytest -m browser                  # dashboard in Chromium, 480 tests, about 7 min (once: uv run playwright install chromium)
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
uv run python scripts/demo_agent_data.py  # regenerate the demo agent's test set (--check in CI)
uv run python -m tests.support.fixture_data --kind inbox  # regenerate a test-fixture agent's data
uv run python scripts/fhir_r4_schema.py   # regenerate the R4 structure rules from HL7's pinned schema
docker compose up --build                 # HAPI FHIR + engine + worker sandbox, on the demo agent
# The one-machine install (deploy/site), run for real with Docker: builds the image, a few minutes.
MENDARI_SITE_E2E=1 uv run pytest tests/test_site_e2e.py -n0 -p no:cacheprovider
docker run --rm -v "$PWD/deploy/site:/mnt:ro" koalaman/shellcheck:stable /mnt/mendari-site /mnt/jobs.sh
# The Hub (hub/, Next.js) against a seeded local registry, as CI's hub-site job: npm ci, lint, typecheck, build, test.
```

## Rules of the codebase

- **No real PHI, ever.** Data is synthetic: the demo agent's comes from
  `scripts/demo_agent_data.py`, the test fixtures' from `tests/support/synthetic_*`.
- **Mendari is a separate project.** Never reuse another project's code, connectors,
  credentials, databases, containers or configuration defaults. Before connecting to a local
  port, check which container owns it (`docker ps`).
- **Agents only.** The product bundles one agent, `agents/portal-message-agent`. The five
  former Decision Packs are test fixtures (`tests/fixtures/reference-agents`, served by
  `tests/conftest.py`; their generators, FHIR records and sandbox are `tests/support/`) and
  live on the branch `archive/decision-packs`. Never add them back to the product, and never
  name one in `src`, `scripts`, `deploy`, the Dockerfile, `docker-compose.yml` or
  `pyproject.toml` (`tests/test_former_packs.py`): a command or script that needs a kind of
  agent the product does not bundle takes `--agent`, and CI gives it the fixture's directory
  (`tests/fixtures/reference-agents/<name>`).
- **One vocabulary.** Everything a person reads or types says "agent", never "pack". The
  rule is `offences` in `tests/test_vocabulary.py`, and four tests hold it, each with a short
  list of what it leaves alone and why (an entry nothing needs any more fails):
  `test_vocabulary.py` (the API descriptions, the product's and the scripts' messages, what
  `mendari new` writes, the Decision Card: add a surface to its `SURFACES`),
  `test_cli_vocabulary.py` (the command line, and that a hidden name is shown nowhere),
  `test_vocabulary_docs.py` (the README, `docs/`, the READMEs, the changelog, `.github/`,
  the deploy templates and compose files) and `test_vocabulary_frontends.py` (the console,
  the Agent Hub, mendari.co).
- **Old names still work, each through one helper.** The manifest is `agent.yaml`, and
  `pack.yaml` still loads (`core/pack.py` `manifest_name`; both in one folder are refused;
  one fixture keeps the old name on purpose, `tests/fixtures/mini-agents/referral-mini`).
  Routes are `/api/agents/...`, `/api/store/agents` and the registry's `/v1/agents/...`; the
  former paths answer the same, outside the API docs (`server/paths.py`: register a renamed
  route with `paths.route`, and read a path through `paths.current` wherever a rule matches
  on it, as `tests/test_legacy_routes.py` checks). Four settings were renamed
  (`legacy.setting`). An archive is written as `.mendari`, and one named `.ohpack` is still
  read. `mendari pull` caches under `agents/`, and a cache under `packs/` is still read
  (`hub/client.py` `caches`). The console's address names the agent as `?agent=` and still
  reads `?pack=` (`dashboard/main.js`); the Hub's page is `/agents/<namespace>/<name>`, and
  `/packs/...` redirects. Not renamed yet: JSON keys (`pack`, `pack_id`), the `pack`
  parameter of `/smart/launch`, database columns and Python identifiers (the Hub asks the
  registry at `/v1/agents/...` since wave 3); a model whose class holds the old
  word is listed in the API description under the title it sets (`server/api_docs.py`
  `TITLED`).
- **The demo agent's data is derived.** `scripts/demo_agent_data.py` writes its `test.jsonl`;
  edit the script, rerun it and commit both. Its canned agent misses paraphrased urgent
  messages on purpose: the evaluation is meant to catch it.
- **Policy only lowers automation.** Caps (per-label, provider failure, risk level) may turn
  AUTO into HUMAN_REVIEW, never the reverse. Risk-level limits live in `core/policy.py`, not
  in manifests (ADR-0002).
- **The action gate lives in `core/policy.py` and only tightens** (ADR-0023): `EFFECT_GATES`,
  `gate_of`, `disposition`. A manifest (`allow_auto: false`), an agent nobody vouched for and
  a second signal that did not agree on a proposal each make a gate stricter; nothing loosens
  one. Mendari authorizes an action and records it, and runs none.
- **A calibration is tied to the agent version it was fitted for** (ADR-0024): when the
  agent's service reports another version, or none, nothing is rescaled and the decision is
  capped. The version cap and the second signal's are `apply_policy`'s step 7, the actions'
  cap its step 8, in that order (the module's docstring; `tests/test_caps_together.py`).
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

See README "Project layout". Spec: `docs/spec/agent-manifest-0.2.md`, with the FHIR mapping of
`agent-manifest-0.3.md` and the actions, calibration and second signal of
`agent-manifest-0.4.md` (the newest, `SPEC_VERSION`; 0.1 to 0.3 still load, and a feature of
one version is asked for with `spec.at_least`, never equality). Decisions: `docs/decisions/`
(ADR-0021: an agent's connection; ADR-0022: the first administrator and the operator
organization; ADR-0023: actions; ADR-0024: a checked confidence and a second signal). The
one-machine install is `deploy/site/` (its script `mendari-site`, its settings made by
`src/mendari/install.py`); the console's Agents, connection and System views are
`dashboard/agents.js`, `connection.js` and `system.js`, over `server/hub_routes.py`,
`connection_routes.py` and `system_routes.py`. Lessons:
`tasks/lessons.md`. Plan and status: `tasks/todo.md` (the current plan at its top, "the
engineering to A+", with the table of names every wave follows; the vision plan below it,
whose Definition of shipped and build prompts are in `notes/vision-audit.md`, Phase 4). Plans,
reviews and the build log: `notes/` (outside the docs site and the sdist; `docs/` is only what
readers of the site need).
