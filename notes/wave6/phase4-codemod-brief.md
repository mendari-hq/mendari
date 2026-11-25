# Wave 6, phase 4.1 in rehearsal: the rename codemod

You write the one-off script that renames "pack" to "agent" across Mendari's code, and you
rehearse it on today's tree so that, when the lead runs it on the final tree, it works the first
time. Mendari is Python 3.11+, uv, pydantic v2, FastAPI,
SQLite and Postgres; console = ES modules in `src/mendari/dashboard`, no build step; Agent Hub =
Next.js in `hub/`; mendari.co = `web/`. You work in your own git worktree.

## Read first

- `CLAUDE.md` in the repository (binding rules; its "Agents only", "One vocabulary" and "Old
  names still work" paragraphs describe what wave 6 removes or rewrites).
- In `notes/wave6/`:
  - `wave6-decisions.md` (it wins over the rest), in particular "Naming" (N1 to N6);
  - `wave6-inventory.md` section 4, "The rename" (4.1 to 4.13): your specification. Where it
    disagrees with the decisions, the decisions win: record reading stays (G6), so
    `site-codes/1` and `ehr-declarations/1` stay as they are.
  - `wave6-build-plan.md`, Phase 4 and "Traps" 1 and 4.

## Where things stand

Base: commit `9dda7607` (run `git reset --hard 9dda7607` in your worktree and check
`git log --oneline -1`). Phases 1, 2 and 3.1 are in it: compatibility names are gone, and the
tests use the fixture agents `tests/fixtures/agents/{triage, clinical-review, chart-summary}`,
the demo agent and `tests/fixtures/mini-agents/{actions,written}-mini`.

At this moment five other engineers are deleting the removed features (hidden sets and the
scoreboard, the Jev/Naive Bayes/CQL/criteria/dosing providers and the training commands, Da
Vinci/PAS/OMOP, the community path, spec versions other than 0.4 with guard and derive). Their
code is still in your tree. Your script must not depend on it: the lead runs your script after
their work is merged, on a tree without those features. Rules about names that only removed
features use are harmless; do not spend time on them.

## The script

`codemod/codemod.py` in the lead's scratch folder, outside the repository (it is attached to
the pull request, never committed). It takes the
repository's path as its argument and:

1. renames the service-side names first (4.10: the contract's `agent_id`/`agent_version` for
   the service become `service_id`/`service_version`, the kit's `Case.agent_id` becomes
   `service_id` before `Case.agent` becomes `agent_id`, a local `agent` that means a service or
   a provider becomes `service`/`provider` before any `pack` becomes `agent`);
2. then applies 4.1 to 4.12 in order: files and modules (`git mv`), Python identifiers, wire
   keys, query and operation parameters, database columns and indexes, event kinds and chain
   subjects, the role, FHIR and CDS identifiers, the archive format, the front ends' reads and
   identifiers and CSS classes, the report and signed formats (every format the first release
   writes is `/1`: the ones the table restarts at `/1` say so in their names);
3. is word-boundary aware and never touches the words of 4.13 (`package*`, `packet*`,
   `unpack*`, `backpack`, `struct.pack`); it renames `repack`, "re-packed" and the local
   `packed` as 4.13 says;
4. touches code, keys, identifiers, paths and machine-read strings; it may also fix prose where
   a rule is mechanical and safe ("a pack" → "an agent", "packs" → "agents" in a docstring),
   but leaves anything doubtful for the prose sweep and lists it;
5. is deterministic, prints what it changed per rule (files, occurrences), and refuses to run
   on a dirty tree;
6. then regenerates the derived files the inventory names (goldens, `task-versions.json`,
   `evaluation_pages.json`, `shown.json`, the terminology `PINNED`, the provider configuration
   schema, the starters' copies of the agent kit, the demo agent's mock and manifest with
   `scripts/demo_agent_data.py`, and whatever else a test pins), each by the repository's own
   generator, never by hand. Find each generator from the test that pins its output.

Changing the script is how you fix what it gets wrong. A hand edit after the script is allowed
only for a case no rule can express safely; list every one (file, line, what and why) so the
lead can repeat it.

## The rehearsal

In your worktree at `9dda7607`: run the script, then run, with at most 3 workers (five other
engineers share the machine):

- `uv run ruff check src tests scripts && uv run ruff format --check src tests scripts`;
- `uv run mypy src tests`;
- `uv run pytest -n 3 -q -p no:cacheprovider`, and the same with
  `MENDARI_TEST_DATABASE_URL=postgresql://postgres:mendari-test@127.0.0.1:56432/mendari_test`
  (Mendari's own Postgres container, already running; "too many clients" is its connection
  limit: rerun that file alone);
- `uv run pytest -m browser -n 3 -q -p no:cacheprovider` (`uv run playwright install chromium`
  once if it is missing);
- in `hub/`: `npm ci`, `npm run lint`, `npm run typecheck`, `npm run build`;
- `uv run mendari demo`;
- the residue: every remaining match of `(?i)pack(?!ag|et)` after removing 4.13's words, in
  code (identifiers, keys, strings) and in prose (comments, docstrings, docs), counted by
  directory, with the code residue listed in full.

Iterate on the script until the checks pass, or until what fails is clearly caused by a
removed feature that the other engineers are deleting (say which). Commit the rehearsal's
result on your branch (`git commit -s`, each message ending with the line
`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`) so the lead can read the diff.
Never push. Never touch production, a registry, Vercel or Neon.

## Hand back

1. The script's path and how to run it; its rules in order, with what each changed in the
   rehearsal (files, occurrences).
2. The derived files it regenerates and how.
3. Hand edits after the script, each with file, line, what and why.
4. The checks with real results.
5. The residue: code in full, prose counted by directory.
6. What the final run must watch for (anything that depends on the tree's state).
7. Your worktree path, branch and `git log --oneline 9dda7607..HEAD`.
