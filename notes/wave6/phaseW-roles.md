# Wave 6, phases 4.2 and 5: the four engineers

Read `phaseW-common.md` first.

## W1. The prose sweep in code (phase 4.2)

**Owns:** every file under `src/`, `tests/` (not the four vocabulary tests, which are W4's),
`scripts/`, `examples/`, `agents/`, `deploy/`, `sandbox/`, `hub/`, `web/`, `.github/`, and
`Dockerfile`, `docker-compose.yml`, `pyproject.toml`, `action.yml`, `app.py`.

**Does:**
- every "pack" left in docstrings, comments, messages, help texts, console and Hub copy, test
  names and test docstrings in those files (the residue report lists them);
- the codemod's doubtful sentences (`--doubts`) and its 53 lines about names and their history
  (`--residue`): reword or delete each, so none is false;
- what phase 1D left naming old formats: messages in `evals/paired.py` and
  `evals/leaderboard.py`, docstrings in `evals/result.py`, `evals/operating_point.py`,
  `evals/share.py`, the comment in `dashboard/report.js`; after the renumbering they read as
  today's format; reword or delete;
- any sentence in those files that says something removed in wave 6 still exists.

**Leaves:** every citation of a spec version or section in code (`spec 0.3 §2.3`,
`agent-manifest-0.4.md`): W3 rewrites them all when the spec becomes one page. Do not edit those
phrases.

## W2. The root files (phase 5A)

**Owns:** `README.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `SECURITY.md`, `GOVERNANCE.md`,
`CHANGELOG.md`, `tasks/lessons.md`.

**Does:**
- `README.md`: what Mendari is today, for the north star in `tasks/todo.md` (developers build
  agents and publish evaluations; hospital IT adopts vetted agents, shadow first, and publishes
  back): its install (the repository is public; the PyPI package `mendari` is being set up and
  is not published yet: say how to install from a clone today, and nothing about a release that
  does not exist), the provider types (`rules`, `llm`, `agent`, `check`), the standards that
  stay (FHIR R4, US Core 6.1.0, CDS Hooks, SMART App Launch and Backend Services, Bulk Data),
  the project layout as it is now, and every link resolving (`tests/test_packaging.py` checks).
- `CLAUDE.md`: rewrite the rules for the code as it is after wave 6: "Agents only" (one bundled
  agent; the tests' synthetic fixture agents in `tests/fixtures/agents`, written by
  `tests/support/fixture_agents.py`); "One vocabulary" becomes one rule held by
  `tests/test_vocabulary.py` alone (W4 writes it: one rule over every tracked text file, outside
  `notes/`, `tasks/`, `licenses/`, vendored folders, lockfiles and binaries); "Old names still
  work" goes (nothing before 0.1.0 is read); the spec is one page,
  `docs/spec/agent-manifest.md`, one `spec_version`, `"0.4"`; the agent and its service
  (`agent_id` vs `service_id`); update every path and name the file cites (check each exists).
- `CHANGELOG.md`: decision O5: the `0.1.0` entry rewritten as what the first release contains,
  agents only, in the file's style; nothing about packs, compatibility or removed features
  (they never shipped). The history stays in git.
- `CONTRIBUTING.md`, `SECURITY.md`, `GOVERNANCE.md`: true of the code after wave 6.
- `tasks/lessons.md`: a lesson from wave 6 if one is worth keeping (the file's format).

## W3. The docs site and the one spec page (phase 5B)

**Owns:** everything under `docs/` except `docs/decisions/`, and `mkdocs.yml`; and, across the
whole repository, every citation of the spec (a version or a section, `agent-manifest-0.x.md`,
`spec 0.3 §5`, "(spec 0.2)") in code, tests, scripts and the console: only those phrases.

**Does:**
- `docs/spec/agent-manifest.md`: one page, the manifest as `spec_version: "0.4"` defines it
  after wave 6: 0.2's base, 0.3's FHIR mapping and record reading, 0.4's actions, calibration,
  second signal and written output, without every removed key and every version rule. Then
  delete `agent-manifest-0.1.md` to `-0.4.md`, update `mkdocs.yml` and every link to them, and
  rewrite every spec citation in the repository to the new page's sections.
  `core/spec.py` is the truth: check every key the page names against it, and every key it
  accepts is on the page.
- every page of `docs/` (guides, operations, architecture, `index.md`, `roadmap.md`,
  `deploying-with-phi.md`, `evaluation-reports.md`, `security/`): true of the code after wave
  6, "agent" everywhere, no removed feature described as present, no compatibility claimed.
  `roadmap.md`'s dated history may stay as history, reworded to "agent".
- `uv run --group docs mkdocs build --strict`, `uv run python scripts/doc_blocks.py run cli`
  (and `run python`, `run install` if they exist), `uv run python scripts/check_doc_snippets.py`.

## W4. The decisions and the one vocabulary rule (phase 5C)

**Owns:** `docs/decisions/` and `tests/test_vocabulary.py`, `tests/test_cli_vocabulary.py`,
`tests/test_vocabulary_docs.py`, `tests/test_vocabulary_frontends.py`.

**Does:**
- `docs/decisions/ADR-0026-one-release-baseline-agents-only.md`, in the ADRs' format: packs
  gone and no compatibility before 0.1.0; the storage baseline and the refusal of pre-release
  databases; every format at `/1`; the agent and its service; one spec version; the removed
  features and why (`notes/wave6/wave6-decisions.md`, G1 to G16).
- every ADR of a removed feature (0007 community agents, 0011 hidden sets, 0013 scoreboard,
  0019 the engine learns; and any other whose decision wave 6 undoes) marked
  "Superseded by ADR-0026" in its status; the word edited out of every ADR (decision O4), the
  decisions' content otherwise left as history.
- `tests/test_vocabulary.py` rewritten as one rule over every tracked text file (`git ls-files`),
  excluding `notes/`, `tasks/`, `licenses/`, vendored folders, lockfiles and binary files:
  it allows the words of inventory 4.13 and nothing else, and also bans the five former ids
  (decision O7). Delete `test_cli_vocabulary.py`, `test_vocabulary_docs.py` and
  `test_vocabulary_frontends.py`, keeping any check of theirs that is not about the word
  (move it into `test_vocabulary.py` or the test file of what it checks).
- the rule will fail on your branch where W1, W2 and W3 have not finished: report exactly what
  it finds (file and line), and make sure that everything it finds is a real offence.
