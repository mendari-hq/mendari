# Wave 6, phase 3.4: the last paths that read data from before the first release

Repository: `the repository` (Python 3.11+, uv, pydantic v2, FastAPI, SQLite and
Postgres; console = ES modules in `src/mendari/dashboard`). You work in your own git worktree.
Run `git reset --hard bad3eb3c` and confirm `git log --oneline -1`.

## Why

The owner decided that nothing made before the first release (0.1.0, not yet published) stays
readable: every format the release writes is `/1`, the database has a baseline marker and a
pre-release database is refused at start (`core/db.py`, `tests/test_schema_baseline.py`), and
"pack" was renamed "agent" everywhere. Read `CLAUDE.md` and `notes/wave6/wave6-decisions.md`
(O1, O2, O10, O11). Code that reads data in a shape only a pre-release version wrote is dead
weight and hides mistakes: a record that lacks a field the release always writes should fail
loudly, not be quietly read another way.

## The paths to judge

Found by the prose sweep (each sentence now says what the code reads; the code still reads it):

- `src/mendari/evals/result.py`: `normalize`, `sweep`, `subgroup_labelled`, `false_automation`
  (fallbacks for results without fields today's results always hold);
- `src/mendari/evals/leaderboard.py` `_question_table`;
- `src/mendari/dashboard/report.js` `runOf` (`?? r`);
- `src/mendari/core/card.py` `_operating_point`;
- `src/mendari/evals/compare.py` `_labelled`;
- rows with a NULL `org` "from before tenancy": `src/mendari/core/audit.py` near lines 958
  and 6018, `tests/test_chain_orgs.py` near 436, `tests/test_access_cli.py` near 70;
- sessions without `iss` in `src/mendari/server/auth.py`;
- `src/mendari/hub/sites.py`'s `db.columns("hub_sites")` check;
- `tests/test_figures.py` near 162.

Search for more of the same kind while you are there (`git grep -n -i -E "before (LP-|v0\.|the
first|tenancy|wave)|older (result|report|record|row|session)|from before|legacy|if .* is None:
# (old|older|before)"` and the like), in `src/` only.

## For each

1. Find what writes the data the path reads, in today's code. If today's code can produce the
   shape the fallback handles (an empty set, a field genuinely optional now, a value a writer
   leaves out on purpose), the path is not a fallback: keep it, and say why.
2. If only a pre-release writer produced it, remove the fallback: read the field as today's
   writer writes it, and let anything else fail as the surrounding code fails today (a
   `MendariError` with fixed words, a 4xx, a validation error), never silently. A database
   column or constraint that only served old rows may be tightened only if the baseline
   schema's `CREATE TABLE` defines it (no migration: there is no released schema to migrate).
3. Update or delete the tests that wrote pre-release shapes to exercise the fallback; keep a
   test that the shape is now refused where refusal is the behaviour.

Keep every security and audit property: the chain, its verification, organization isolation,
PHI handling. If removing a path would weaken any check, keep it and say so.

## Rules

- Only these paths and what they force. Do not rename, reword prose beyond the lines you
  change, or touch `docs/`, `README.md`, `CLAUDE.md`, `CHANGELOG.md`, `docs/decisions/` or
  the four vocabulary tests (four other engineers are editing them right now).
- Commit after each path: `git commit -s`, each message ending with the line
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Never push. Never touch
  production, a registry, Vercel or Neon.
- Checks with real results, at most 3 workers: the tests of what you changed on SQLite and on
  Postgres (`MENDARI_TEST_DATABASE_URL=postgresql://postgres:mendari-test@127.0.0.1:56432/mendari_test`);
  `uv run pytest -m core -q -p no:cacheprovider`; ruff check and format check on
  `src tests scripts`; `uv run mypy src tests`; if you changed the console, the browser tests of
  what you changed; at the end the whole suite once with `-n 3`.

## Hand back

Worktree path, branch, `git log --oneline bad3eb3c..HEAD`; per path: removed or kept, and why
(what writes that shape today, if anything); tests changed or deleted; the checks with real
results.
