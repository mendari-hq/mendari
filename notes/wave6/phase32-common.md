# Wave 6, phase 3.2: what every engineer follows

You are one of five engineers working at the same time on "wave 6: packs gone" for Mendari
(Python 3.11+, uv, pydantic v2, FastAPI, SQLite and Postgres; the console is ES modules with no
build step, in `src/mendari/dashboard`; the Agent Hub is Next.js in `hub/`). You work in your own git worktree of the repository.

The owner decided: the former Decision Packs and everything only they used go; the
infrastructure agents need stays; nothing made before the first release stays compatible; old
data is replaced by new synthetic data. Phases 1, 2 and 3.1 are done: the former packs, their
data, their generators and their tests are gone, compatibility names are gone, and the tests
decide with three synthetic fixture agents (`tests/fixtures/agents/{triage, clinical-review,
chart-summary}`, written by `tests/support/fixture_agents.py`), the bundled demo agent
(`agents/portal-message-agent`) and two minis (`tests/fixtures/mini-agents/{actions,written}-mini`).

What is left in this phase is the code of the removed **features**. Each engineer owns one
feature group, everywhere it appears: `src`, the tests, the console, the docs pages, CI, scripts.

## Read first

- `CLAUDE.md` in the repository: binding rules. Its paragraphs "Agents only", "One vocabulary"
  and "Old names still work" describe what is being removed or rewritten; the rest binds you.
- In `notes/wave6/`:
  - `wave6-decisions.md`: the lead's decisions G1 to G16. **It wins** over the other two files.
    Note G6 (record reading stays, with `code_systems`), G10 (the bounded engine stays), G13
    (the labeller role, its helpers and the blind grading screen stay).
  - `wave6-inventory.md`: sections 1.1 to 1.11 (what each module loses), 1.15 to 1.18. It was
    written before the decisions: where it says REMOVE for something G6 keeps, keep it.

## Base

Run `git reset --hard 9dda7607` in your worktree and confirm with `git log --oneline -1`.

## Your job

Remove your feature group completely, and only it:

- its modules, functions, classes, routes, commands, settings, events, formats, console views
  and docs pages; every mention of it in what stays (help texts, messages, docstrings, docs
  pages, the API description, `mkdocs.yml` nav, CI steps, scripts, `pyproject.toml`);
- the tests of what you remove, wherever they are; keep and fix every test of what stays;
- its rows and citations in `docs/security/threat-model.md` and `docs/security/review-packet.md`
  (do not renumber the rows that stay). `tests/test_threat_model.py` pins the "Agent content"
  provider tables to the code and fails when the model cites a test that does not exist.

Other engineers remove other features in the same files (`cli.py`, `server/app.py`,
`settings.py`, `core/spec.py`, `core/audit.py`, the threat model, `mkdocs.yml`,
`tests/conftest.py`, `tests/test_cli_errors.py`, `tests/test_auth.py`, ...). The lead merges the
five branches, so:

- **Touch only the lines of your feature** in a shared file: no reformatting, no reordering, no
  tidying of anything else, no renames.
- **A module may be deleted** when nothing that stays imports it. If a module of another
  group's feature still imports it in your tree, leave it in place, and list it in your hand-back
  (the lead deletes it after the merge).
- **Do not rename anything that says "pack"** (identifiers, keys, columns, files): a codemod
  renames everything at once in phase 4. Write today's names in new or edited code. Prose you
  rewrite anyway says "agent".
- **Do not edit** `README.md`, `CLAUDE.md`, `CHANGELOG.md`, `tasks/`, `notes/`,
  `docs/decisions/` (ADRs) or the `docs/spec/agent-manifest-*.md` pages, unless a test fails
  without it; phase 5 rewrites them. Say in your hand-back what they still say about your
  feature.
- `tests/conftest.py`: remove only your feature's helpers and settings.

## Rules

- Work only in your worktree. Never push. Never touch production, a registry, Vercel or Neon.
- Commit after every coherent piece, even marked "(work in progress)": `git commit -s`, each
  message ending with the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Postgres for tests is Mendari's own container, already running:
  `MENDARI_TEST_DATABASE_URL=postgresql://postgres:mendari-test@127.0.0.1:56432/mendari_test`.
  "too many clients" is its connection limit: rerun that file alone.
- Checks, with real results in your hand-back:
  - the test files you changed, on SQLite and on Postgres;
  - `uv run pytest -m core -q -p no:cacheprovider`;
  - `uv run ruff check src tests scripts && uv run ruff format --check src tests scripts`;
  - `uv run mypy src tests`;
  - at the end, the whole suite once: `uv run pytest -n 3 -q -p no:cacheprovider` (four other
    engineers share the machine: never more than 3 workers);
  - if you changed the console: `uv run pytest -m browser -n 3 -q -p no:cacheprovider` on the
    files you changed (`uv run playwright install chromium` once if it is missing);
  - if you changed `docs/` or `mkdocs.yml`: `uv run mkdocs build --strict`.

## Hand back

1. Your worktree path, branch and `git log --oneline 9dda7607..HEAD`.
2. What you removed, by file (lines removed and added).
3. Leftovers for the lead: what you could not remove because another group's code still uses
   it, with the user.
4. Tests deleted (file and function) and why; tests changed and why.
5. The checks, with real results (counts, failures with their first line).
6. What README, CLAUDE.md, CHANGELOG, ADRs and the spec pages still say about your feature.
7. Anything not done, and why.
