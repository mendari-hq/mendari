# Wave 6, phases 4.2 and 5: the words (what every engineer follows)

You are one of four engineers finishing "wave 6: packs gone" for Mendari (Python 3.11+, uv,
pydantic v2, FastAPI, SQLite and Postgres; the console is ES modules in `src/mendari/dashboard`,
no build step; the Agent Hub is Next.js in `hub/`; mendari.co is `web/`; the docs site is
MkDocs over `docs/`). You work in your own git worktree of the repository.

## What happened before you

The owner decided that the former Decision Packs and everything only they used go, that the
infrastructure agents need stays, that nothing made before the first release (0.1.0, not yet
published) stays compatible, and that the governed unit is called an **agent** everywhere.

- Phases 1 to 3 removed the former packs, their data, every compatibility name, and these
  features: hidden evaluation sets and the scoreboard; the providers `jev`, `naive_bayes`,
  `cql`, `criteria`, `dosing`, `any_of`, `cascade`, `vote`; `mendari train`, `improve`,
  `agreement`; Da Vinci CRD/DTR/PAS, X12, OMOP; the community contribution path (the bounded
  engine stays); every spec version but `"0.4"`; `guard`, `derive`, a policy as a separate
  file; the fictional code systems.
- Phase 4.1, a codemod, renamed "pack" to "agent" in code: identifiers, modules and files,
  JSON keys, database columns, event kinds (`store_version.*`), the role `agent_author`, FHIR
  identifiers (`agent-version`), the archive format (`mendari-agent/1`), every format at `/1`.
  The process that answers for an agent is its **service** (`service_id`, `service_version`,
  `service`); the governed unit is the **agent** (`agent_id`, `agent_version`, `agent_digest`).
  The codemod also rewrote prose where a rule was safe, and left the rest to you.

## Read first

- `CLAUDE.md` (binding; its "Agents only", "One vocabulary" and "Old names still work"
  paragraphs are out of date: engineer W2 rewrites them).
- `notes/wave6/wave6-decisions.md` (the decisions, which win), `notes/wave6/wave6-inventory.md`
  section 4.13 (the words that may hold the letters: `package*`, `packet*`, `unpack*`,
  `backpack`, `struct.pack`).
- The codemod's residue report and its list of doubtful sentences: run
  `python3 codemod.py . --residue` (the codemod, attached to the pull request)
  and `... --doubts --dry-run` in your worktree (both write nothing). They list, by file and
  line, every remaining "pack" and every sentence about names the rename may have made false.

## Base

Run `git reset --hard 61599b6d` and confirm `git log --oneline -1`.

## The rules

- The word: an **agent**, never "pack", "Decision Pack" or "pack's" anywhere a person reads or
  types, except the words of 4.13. A sentence is rewritten so it reads well, not patched: "a
  pack" becomes "an agent", and a sentence about "the old name still works" is now false and
  goes. Nothing claims compatibility with anything before 0.1.0.
- The five former ids (`patient-inbox-router`, `referral-router`, `prior-auth-preflight`,
  `clinical-trial-prescreener`, `ai-guard`) are named nowhere outside `notes/` and `tasks/`.
- Facts only. Every claim about the code is checked against the code; every number is
  measured; nothing is promised that is not built. Plain, short sentences; the house style of
  the file you edit.
- You own only the files your brief lists. Four engineers are editing the rest. Never push.
  Never touch production, a registry, Vercel or Neon.
- Commit after every coherent piece: `git commit -s`, each message ending with the line
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Checks (real results in your hand-back), with at most 3 workers: the tests that cover what
  you changed; `uv run ruff check src tests scripts && uv run ruff format --check src tests
  scripts`; `uv run mypy src tests`; for docs, `uv run --group docs mkdocs build --strict`;
  at the end, `uv run pytest -n 3 -q -p no:cacheprovider` once. Postgres, if you need it:
  `MENDARI_TEST_DATABASE_URL=postgresql://postgres:mendari-test@127.0.0.1:56432/mendari_test`.

## Hand back

Worktree path, branch, `git log --oneline 61599b6d..HEAD`; what you changed, by file;
what you left and why; the checks with real results.
