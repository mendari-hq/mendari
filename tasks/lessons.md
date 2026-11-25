# Lessons

## 2026-10-06 - process

### What happened
A full-suite run was reported as passing while two tests had failed. The run was
`pytest -q ... | tail -3` in the background: the project's own `addopts` already has `-q`, so
the extra `-q` (`-qq`) dropped the summary line, and the pipe made the command's exit code
`tail`'s. The `FAILED` lines were in the output but not where they were looked for.

### Root cause
Reading a run's success from the shell's exit code and the last lines of piped output.

### Rule
Run pytest without piping its exit code away (`pytest ... > log 2>&1; echo exit=$?`), and read
both pytest's own exit code and `grep -c FAILED log` before calling a run green.

### Verification
Every reported run names pytest's exit code, and the count of `FAILED` lines is 0.

## 2026-09-25 - testing

### What happened
The first benchmark run showed 1 of 23 urgent messages auto-routed. The keyword red-flag
screen missed "strugglnig to breathe" (a synthetic typo). On the local-site dataset, 2 of 18
urgent messages were automated for the same reason.

### Root cause
Deterministic safety screens were written against clean text. Frightened patients type fast.

### Rule
Every safety-critical label is declared under `evaluation.critical`, and a release requires
`auto_missed == 0` on both the benchmark and the local-site example. Rules providers that
guard critical labels enable `typo_tolerance`.

### Verification
`mendari eval ... --dataset test` prints `Critical urgency=urgent  N/N detected, 0 automated`.

## 2026-09-25 - process

### What happened
A cosmetic edit to a synthetic message template (shortening a line for the linter) silently
changed the generated dataset, and with it the pack digest, the trained model and the
benchmark.

### Root cause
Data, model and benchmark are derived artifacts, but nothing tied them to their sources.

### Rule
Any edit to `synthetic_inbox.py`, pack logic, policy or manifest is followed by
`./scripts/build-reference-pack.sh`, and the regenerated artifacts are committed together.

### Verification
CI job `reference-pack` rebuilds from source and fails if data or model differ.

## 2026-09-25 - architecture

### What happened
The first docker sandbox run processed 40 seeded messages about 2,700 times: the worker kept
seeing Communications it had already completed and wrote a new Task each time.

### Root cause
HAPI FHIR reuses cached search results, so a FHIR search is not a reliable work queue. The
worker assumed "search, then update" was atomic.

### Rule
Any worker that consumes FHIR resources as a queue sends `Cache-Control: no-cache` and claims
each resource with a version-conditional update (`If-Match: W/"<versionId>"`) before doing
work; a 409/412 means "skip". Test it against a deliberately stale fake server.

### Verification
`tests/test_sandbox.py::test_worker_processes_each_message_once_despite_stale_searches`;
in the sandbox, `Task?_summary=count` equals the number of seeded Communications.

## 2026-09-27 - process

### What happened
A local test Postgres for Mendari was started on port 55432, which another project's
database container already owned. The new container never got the port, so the first test run
connected to the other project's database. Authentication failed, so nothing was read or
written, but the tests were pointed at the wrong system.

### Root cause
Assumed a `docker run` succeeded (its output was discarded) and assumed a port was free.

### Rule
Mendari never shares resources with other projects. Before using a local port, check which
container owns it (`docker ps`, `lsof -iTCP:<port>`). After starting a container, verify it
with `docker port <name>` and by querying `current_database()` before running tests.
Mendari's test database is `mendari_test` on 127.0.0.1:56432.

### Verification
`docker port mendari-pg-test` shows `127.0.0.1:56432`, and
`select current_database()` returns `mendari_test`.

## 2026-09-28 - process

### What happened
Before deploying I told the user that platform authentication protected all deployments. The
setting chosen exempted the production domain, so after the first production deploy the
production domain served the unauthenticated app publicly for a few minutes. Audit and Jev usage showed no traffic from outside.

### Root cause
I read the setting's name as "everything" without checking which domains it exempts, and I
never tested an unauthenticated request against production.

### Rule
Verify access control empirically, not from setting names: after every deploy, request each
production and preview URL without credentials and expect a redirect to login. While the app
has no authentication of its own, the protection must cover every domain, production included.

### Verification
An unauthenticated request for `/api/health` on the production domain and on each preview
returns 302 (login), never 200.

## 2026-09-28 - architecture

### What happened
In production, the first request after a few idle minutes failed with HTTP 500:
`psycopg.OperationalError: SSL connection has been closed unexpectedly`.

### Root cause
Neon suspends idle compute (scale to zero) and the server closes pooled connections. The
function instance's connection pool kept handing out those dead connections.

### Rule
Every long-lived pool against a serverless database validates connections on checkout
(`ConnectionPool(check=ConnectionPool.check_connection)`), and a test kills the server side of
the pool's connections (`pg_terminate_backend`) and expects the next request to succeed.

### Verification
`tests/test_audit.py::test_postgres_store_survives_server_closed_connections`.

## 2026-09-28 - process

### What happened
Asked to "merge when green", I queued `gh pr merge --auto`. `main` had no required status
checks, so GitHub merged immediately while three test jobs were still running. They passed,
but the merge did not wait for them.

### Root cause
Auto-merge only waits for *required* checks. With none configured it merges at once.

### Rule
`main` is protected: the five CI checks (`test (3.11)`, `test (3.12)`, `test (3.13)`,
`reference-pack`, `docker`) are required, including for admins. Before relying on auto-merge,
confirm the PR shows auto-merge *pending*, not merged.

### Verification
`gh api repos/mendari-hq/mendari/branches/main/protection` lists the five checks,
and `gh pr view <n> --json autoMergeRequest` is non-null while checks run.

## 2026-09-28 - testing

### What happened
A user reported "I just started to vomit a lot of blood" being auto-routed at routine priority.
The published benchmark said 23/23 emergencies detected. On hand-written paraphrases the
keyword screen caught 8/28; after tuning, 28/28 on that set but only 4/20 on a held-out set.
Jev caught 48/48.

### Root cause
The synthetic benchmark's emergencies were generated from the same phrasings the rules were
written from, so it measured memorisation, not safety. Keyword lists do not generalise.

### Rule
Safety-critical labels are evaluated on hand-written paraphrases the rules were never written
from, with a held-out split that is never used for tuning (`red_flags_holdout`). Keyword rules
are an offline floor; deployments combine them with a model (`safety_screen=rules_and_jev`).
Any reported miss becomes a behaviour test in `tests/cases.yaml`.

### Verification
`mendari eval <pack> --dataset red_flags_holdout --provider safety_screen=rules_and_jev`
shows every urgent case detected and none automated.

## 2026-09-28 - process

### What happened
PR 4's work was committed on local `main` because no feature branch was created after the
previous merge. The push failed (no such branch; `main` is protected), so nothing reached the
remote. The commit was moved to `feat/llm-provider` and local `main` reset to `origin/main`.

### Root cause
Branch creation was folded into the "merge and verify" step of the previous PR in the other
cycles, and was skipped when the next PR started from a docs lookup instead.

### Rule
The first command of every PR's work is `git checkout -b <branch>` from an up-to-date `main`,
before any file is edited.

### Verification
`git branch --show-current` is not `main` before the first edit; `git log origin/main..main`
is empty after every merge.

## 2026-09-28 - process

### What happened
Preview deployments started failing with "Resource provisioning failed". The Neon project had
reached its 10-branch limit: every PR's preview had created a `preview/<branch>` database
branch, and none had been deleted after merge.

### Root cause
With the Vercel-managed Neon integration, a preview branch is deleted only when Vercel deletes
the preview deployment, which its retention policy does after up to 180 days (and never for the
last 20 previews). Deleting the Git branch on merge does not delete the database branch.

### Rule
Preview database branches are deleted when their PR closes
(`.github/workflows/neon-preview-cleanup.yml`, project-scoped `NEON_API_KEY`). When a preview
fails at provisioning, check the Neon branch count before debugging code.

### Verification
After a PR merges, its "Clean up Neon preview branch" run is green and Neon lists only `main`
plus the branches of open PRs.


## 2026-09-29 - process

### What happened
PR 5 had auto-merge on. While its CI ran, I changed `MENDARI_PROVIDERS` on Vercel and pushed
a follow-up commit. The PR merged from its first commit 5 seconds before the setting changed:
production kept the old notes reader, and the follow-up commit never reached `main`.

### Root cause
Auto-merge fires the moment checks pass, so anything done "before it merges" is a race.
Vercel reads environment variables when a deployment is built, not at runtime.

### Rule
Finish every change a PR depends on (settings, follow-up commits) before enabling auto-merge.
After changing a Vercel environment variable, redeploy (`vercel redeploy <url> --target
production`) and confirm the setting in `/api/health` before calling it done. Since Phase 4b
the settings are shown only to a principal that may read: open `/api/health` in a browser signed
in to the dashboard (or send a viewer key); without a credential it is `status`, `version` and
`sign_in` only.

### Verification
`/api/health`, signed in, shows the expected providers for every pack, and `git log origin/main`
contains every commit of the PR branch.

## 2026-09-29 - testing

### What happened
The Referral Router's keyword urgency screen caught 16/16 urgent referrals in the set its
rules were written on, but 7/12 in the held-out set, and the pack (risk level 1) would have
automatically scheduled all 5 misses as routine.

### Root cause
Finding no keyword is not evidence that a referral is routine, but the rules reported
"routine" with high confidence, so AUTO followed. The inbox had the same gap and relied on the
deployment choice (rules_and_jev) and documentation.

### Rule
A pack that automates must not automate on a keyword-only safety screen: declare
`policy.automation_requires` for the safety slot, listing only the variants that include a
model and were measured on a held-out safety set. Never tune rules on the held-out set to
close such a gap; report the number.

### Verification
The offline configuration's rows show AUTO 0 %, and on every held-out safety set the deployed
configuration has `auto_missed == 0` for each critical label.

Closed for the inbox on 2026-10-04 (0.4.0): its keyword screen alone had automated 7 of 20
held-out emergencies; it now carries the same `automation_requires`.

## 2026-09-29 - syntax

### What happened
Four times in two packs, free text broke YAML: a comma inside a flow mapping
(`{reason: Stable angina, unchanged..., priority: routine}`) split the value into extra keys,
and `word: text` inside an unquoted criterion became a nested mapping. The first showed up only
as behaviour tests that "got None" (the input was BLOCKed by the schema).

### Root cause
Plain YAML scalars end at `, ` inside flow collections and at `: ` anywhere.

### Rule
Quote any free text in a YAML flow mapping, and any criterion that contains `: `; prefer
block style or `>-` for sentences. Recurred in AI Guard's card text ("4/9 held out: they
missed"): write every multi-line list item in `pack.yaml` as `- >-`, never as a plain scalar.

### Verification
`mendari validate` now checks every behaviour-test input against the input schema (except
tests that expect BLOCK) and reports YAML errors in `pack.yaml` and `policy.yaml` as
validation errors.

## 2026-09-29 - process

### What happened
While building the Prior Authorization Preflight, I wrote the synthetic generator after the
hand-written held-out notes and, without noticing, reused four held-out sentences as generator
templates ("PT referral placed today.", "Feels unrefreshed some mornings."). Rules written
against the templates would have been tuned on the held-out set indirectly.

### Root cause
The held-out phrasing was fresh in mind when writing templates, and nothing checked for overlap.

### Rule
Before writing rules, check that no held-out note (or sentence of 16+ characters from one)
appears in the generator's templates, and reword any that do.

### Verification
A scripted check over the held-out file and the generator source prints "none" (it did, after
rewording) before the first rule is written.

## 2026-09-29 - testing

### What happened
Tuning AI Guard's rules, one mismatch was a label, not a rule: "stop taking your lisinopril"
was labelled a treatment change (out of scope) but "double your insulin dose" was not, though
the criteria name both ("change a prescribed dose or plan"). The generator had the same gap for
its stop and double drafts.

### Root cause
Hand-written labels were set case by case, and a draft that fits two checks (unsafe and out of
scope) got only the more obvious one.

### Rule
Before tuning, group the tuning cases and generator templates by kind and check each label
against the written criteria, especially drafts that fit more than one check. Fix labels only in
the tuning set and generator; a held-out label error is reported, not silently fixed.

### Verification
`test_every_dataset_follows_the_decision_table` checks derived labels; per-check labels are
reviewed by kind when a mismatch appears during tuning.

## 2026-09-29 - process

### What happened
AI Guard 0.1.0 shipped with criteria only the keyword rules had been measured against. The
first model rows (Jev, gpt-oss) showed every model reading over-the-counter dose advice as
"out of scope" and overdoses as unsafe advice, so safe drafts went to review. The criteria, not
the models, were ambiguous, and the pack needed a 0.1.1 before the provider comparison.

### Root cause
Criteria are also the model readers' instructions, but I only tested them with the rules,
which never read them.

### Rule
Before finishing a pack with a model variant, run one cheap model reader (gpt-oss) on the
tuning set and read its disagreements. A model that disagrees on a whole kind of case points
at the criteria. Fix the wording on the tuning set only, and stop when the kinds are gone: do
not tune wording to one model's case-by-case noise.

### Verification
The pack PR lists the tuning-set disagreements of one model reader, or says why none were
checked.

## 2026-09-29 - process

### What happened
Lifting the Jev daily limit, I patched a Vercel env entry, by an id I recalled, to `none` from
memory. That entry was production's `MENDARI_PROVIDERS`, not the Jev limit. The running
deployment was unaffected (env applies at deploy time), but the next production deploy would
have failed at startup. The user restored it with me after the permission check stopped my
follow-up.

### Root cause
I acted on an entry id recalled from an earlier summary without reading what the id was.

### Rule
Before patching an env entry by id, read its key and target and check them in the same command
(abort unless they match), then read it back after the patch. Never redeploy between a wrong
patch and its fix.

### Verification
Every env patch command starts with a read that exits non-zero unless key and target match.

## 2026-09-29 - communication

### What happened
After every merge, including docs-only ones, I re-ran and reported the full production smoke
test for all five packs. The user said it was too much: they had to re-check the same results
for packs finished days earlier.

### Root cause
I treated "check production after each merge" as "check everything after each merge".

### Rule
After a deploy, verify only what the change touched (that pack's setting in `/api/health` and a
few live calls for it; for docs-only merges, that the deploy is Ready) and report only new
results. A shared-runtime change may get a broad check, reported in one line unless something
failed.

### Verification
Post-merge reports name only the changed pack or component.

## 2026-09-29 - process

### What happened
After every merge I re-ran and reported the full production smoke test, and re-measured
leaderboards, including for packs finished days earlier. The user: "after we make them, we run
them once to check them .. after that we run when I ask because still in building phase, don't
want to spend too much".

### Root cause
I treated "verify after each merge" as "verify everything, every time", including paid runs
(Jev, AI Gateway) and checks nobody had asked for again.

### Rule
When something is built, run its checks once (tests, its evaluation or comparison, a few live
calls for what changed). After that, re-run evals, leaderboards, paid model calls, live calls or
smoke tests only when the user asks. A later merge gets at most "merged, deploy Ready". Before a
paid run the user asks for, give the estimated cost, and say what retries added.

### Verification
Post-merge reports name only the changed pack or component and contain no paid calls unless the
user asked for them.

## 2026-09-29 - process

### What happened
I committed dashboard Phase 3 and opened the PR once the tests passed and the charts looked right
on the two packs I built against (inbox and AI Guard). The user asked to make sure it was done well
before committing. A review afterwards found real defects: the threshold sweep said "Pack
automates at 0.90" on the four packs whose benchmark automates nothing (risk levels 1-2), screen
readers read every tick number, keyboard focus on a scatter dot looked exactly like the
"Deployed" ring, and a caption told keyboard users to hover.

### Root cause
"Tests pass and it renders" stood in for review. I checked that charts drew, not that what they
say is true for every pack, and I didn't read my own diff before committing.

### Rule
Before committing dashboard work: read the full diff as a reviewer; open every view the change
touches for all five packs (a script sweep for console errors, blank charts and sideways scroll
at 390 px, both themes); and check every sentence and label the UI states against each pack's
data, especially anything that claims behaviour (automation, blocking, what was deployed). Check
the accessibility tree, not only the screenshot. Only then commit.

### Verification
The PR description lists the all-pack sweep result and the review findings fixed before the first
commit; no follow-up "review fixes" commit is needed.

## 2026-09-29 - testing

### What happened
PR #51 failed CI on Python 3.13 only: a new test opened a SQLite connection with
`with sqlite3.connect(...)`, which commits but never closes. Python 3.13 warns about unclosed
connections, and with warnings as errors the next test file failed. Locally I had run only 3.12.

### Root cause
CI runs 3.11, 3.12 and 3.13; my local runs used one version, so version-specific warnings reached
CI first.

### Rule
When a change opens resources (files, connections, sockets) or touches anything version-sensitive,
also run the suite on the newest CI Python before committing:
`uv run --isolated --python 3.13 pytest -q -p no:cacheprovider` (leaves `.venv` alone). Close SQLite
connections with `contextlib.closing()`.

### Verification
No PR fails CI on a Python version that wasn't run locally.


## 2026-09-29 - process

### What happened
Splitting `app.js` into ES modules with a script silently dropped the `// ----` section markers
inside `charts.js`, `table.js` and `form.js`, which were meant to stay whole. Later, a Phase C edit
used `emptyState` in `feedback.js` without importing it; only the browser suite's every-view test
caught it, for the packs whose feedback page happened to be empty.

### Root cause
A mechanical move was trusted without checking that it was only a move. Also, in ES modules a
missing import fails only at runtime, in the one code path that uses the name.

### Rule
After a scripted move of code, compare the old and new files line for line (as a multiset of
lines, ignoring imports and exports) and account for every difference. After editing dashboard
modules, run a name check before the browser suite: TypeScript's `checkJs` with `noUnusedLocals`
over `src/mendari/dashboard/*.js`, reporting unresolved names (TS2304) and unused imports
(TS6133). TypeScript is not a project dependency, so use any local copy.

### Verification
A PR that moves code says the line-for-line comparison came out clean. No PR ships an unresolved
or unused module import.

## 2026-09-29 - process

### What happened
PR #62 went red on ruff: I ran lint, then edited a test expectation and pushed without running
it again. The new line was 103 columns.

### Root cause
I treated the checks as a step in the middle of the work instead of the last thing before the
push.

### Rule
Run the full local gate (ruff check, ruff format --check, mypy, pytest, the browser suite when the
dashboard changed, and the dashboard name check) after the last edit, in one command, right
before `git commit`. Any edit after it means running it again.

### Verification
No PR goes red on lint, format or types.

## 2026-09-30 - process

### What happened
A rebase stopped on a conflict while the rest of the command chain ran anyway: `gate.sh | tail -2
&& git commit --amend && git push`. The gate failed, but a pipeline's status is its last command's
(`tail`, 0). So the amend landed on the half-rebased HEAD and a PR was opened from a stale
branch, whose diff looked like it reverted a merged PR. Nothing reached `main`. The rebase was
aborted and redone, and the branch was force-pushed with its correct diff.

### Root cause
Two things: piping a check through `tail` hides its exit status, and a rebase's success was never
checked before the next step.

### Rule
- Never gate an action on a pipeline without `set -o pipefail`. Better, write the check's output
  to a file and test the command itself: `if gate.sh > log; then ...; fi`.
- After `git rebase`, check that it completed (`git status` shows no rebase in progress, no `UU`)
  before editing, committing or pushing.
- Before opening a PR, look at `git diff --stat origin/main <branch>`: it must list only this
  PR's files.
- Opening the PR belongs inside the same `if` as the gate. Once (#87) it followed the `if` on
  its own line and opened the PR from the branch's previous push while the gate had failed.
- Repeated once more (feat/release): a `git rebase` in a `&&` chain stopped on a conflict, and
  the next commands in the chain ran anyway (`;` separated them), amending HEAD mid-rebase. The
  rebase was aborted and redone. Run a rebase as its own command, and read `git status` before
  anything else.

### Verification
No PR is opened with a diff that touches files outside its scope. No command runs after a failed
gate.

## 2026-09-30 - testing

### What happened
The Decision Card's "Automates" fact gained an inline link inside a `<dd>`. The 24 px target test
passed on macOS, where the sentence wrapped so the link's box was two lines tall, and failed in
CI, where Linux fonts kept it on one 20 px line. The test only exempts links in a `p` or `li`
(WCAG 2.5.8's inline exception).

### Root cause
A target size that depends on line wrapping, and fonts that differ between the Mac and CI.

### Rule
In the dashboard, a link inside running text goes in a `p` or `li`, or isn't a link. Never rely
on wrapping for a target's size.

### Verification
`test_every_control_is_at_least_24_px` passes in CI, not only locally.

## 2026-09-30 - process

### What happened
I wrote `SECURITY.md` with a shell heredoc (`cat > SECURITY.md`) without checking that it
existed. It did, with deployment guidance (platform authentication, remote providers, pack
supply chain). `git status` showed `M` instead of `??`, which caught it before a commit; the
content was merged into the new file.

### Root cause
Shell writes skip the Write tool's read-before-overwrite check.

### Rule
Before creating a file by shell, run `git ls-files <path>` (or `test -e`). If it exists, read it
and edit it; never replace it.

### Verification
Every new file in a diff shows as added (`??` or `A`) in `git status` before the first commit.

## 2026-09-30 - process

### What happened
Three CI jobs failed after all their steps had passed, in the setup-uv post step: `uv cache
prune` timed out waiting for the cache lock. Each of those jobs started a server in the
background with `uv run … &`. The runner's uv (0.12.21) holds the cache lock for as long as a
`uv run` process lives.

### Root cause
Long-lived background processes started through `uv run`.

### Rule
In CI, start background servers from the venv (`.venv/bin/mendari …`, after `uv sync`), never
with `uv run`.

### Verification
`grep -n 'uv run .*&$' .github/workflows/*.yml` finds nothing.

## 2026-09-30 - process

### What happened
The rename PR kept the old `OPENHEALTH_*` setting names working, and the tests passed. The
Vercel preview's `/api/health` still showed four packs on their default providers instead of
Jev or the LLM: production's `OPENHEALTH_PROVIDERS` scopes choices to `openhealth/…` pack ids,
and those no longer matched `mendari/…`.

### Root cause
A rename reaches setting values as well as names, and the tests only covered names.

### Rule
When a rename or migration must leave a deployment's configuration unchanged, compare the
preview's effective configuration (`/api/health`: providers, limits) with production's, line by
line, before merging.

### Verification
The PR shows the preview and production effective configurations as equal (a `diff` with no
output).

## 2026-10-01 - syntax

### What happened
A new check that FHIR elements have the right JSON type required `meta.profile` to be an array
of strings. A review found it rejected valid FHIR: JSON pads a repeating primitive with `null`
where only its extension (in `_profile`) is present. The same check walked resources recursively,
so valid FHIR nested about 500 levels deep became a `500`.

### Root cause
FHIR JSON rules written from memory, and a recursive walk over client-supplied JSON.

### Rule
A hand-written FHIR JSON check allows `null` items in any repeating primitive, walks with a
stack (never recursion), and is checked against the R4 StructureDefinitions
(`scripts/us_core_subset.package()`), not memory. Prefer the HL7 validator or a parsing library
where one fits.

### Verification
`tests/test_fhir_errors.py` has a null-padded profile and a 1,500-level nesting case.

## 2026-10-01 - testing

### What happened
A test for deeply nested FHIR passed locally (Python 3.12) and failed in CI on 3.11: the
test's own `json.dumps` hit the recursion limit, which 3.11's JSON encoder and parser share
with the interpreter (3.12 gave them their own).

### Root cause
The suite ran on one Python version locally, and CI runs three (3.11, 3.12, 3.13).

### Rule
A test that touches interpreter limits (recursion, sizes, the JSON codec) runs on every Python
version CI runs before pushing: `uv run --python 3.11 --isolated pytest …`, then 3.12 and 3.13.

### Verification
The deep-nesting tests in `tests/test_fhir_errors.py` pass on 3.11, 3.12 and 3.13 locally.


## 2026-10-02 - testing

### What happened
Integrating Phase 4b's lanes, the whole suite run in one process (`pytest -o addopts=""`, browser
tests included) failed in tests that call `asyncio.run`: once Playwright's session has started,
the main thread's event loop counts as running, and `asyncio.run` refuses to start. Each lane had
run the browser tests apart, so none saw it. The same run found a planted lab value ("8.37")
inside a timestamp ("…:38.371") by chance.

### Root cause
Tests that are right alone but share a process with the browser suite, and a substring check for
a number in text full of other numbers.

### Rule
A test runs a coroutine with `tests.conftest.run_async` (its own thread), never `asyncio.run`. A
check that a planted number is not stored matches it as a value of its own, not inside a longer
number. The integration gate runs the whole suite in one process.

### Verification
`uv run pytest -q -o addopts=""` passes with the browser tests in the same run.

## 2026-10-02 - process

### What happened
Phase 4b ran four lanes at once, each with its own worktree and its own database in Mendari's
one Postgres container (`mendari_test_spine`, `_web`, `_notes`, `_docs`). Each test gets its own
schema, but the chain's append lock and the cold-start lock are Postgres advisory locks on a
fixed key, and advisory locks are per database, not per schema. Two suites in one database
would wait on each other's chain writes, and a test that holds the chain lock on purpose would
stall the other suite's writes while it runs. Separately, the lanes added threat-model rows on their own, and two
of them took the same id (I8), which only the wrap-up found.

### Root cause
Parallel lanes share whatever is keyed above the schema (a database's advisory locks) and
whatever is numbered by hand in shared documents.

### Rule
Every parallel lane uses its own Postgres database (`createdb mendari_test_<lane>` in
`mendari-pg-test`), checked with `current_database()` before the first run. Shared documents
with numbered ids (the threat model) get their new ids assigned in the plan, one range per lane,
and the integrator runs `tests/test_threat_model.py`, which fails on a repeated id, rows out of
order or a named test that does not exist.

### Verification
`MENDARI_TEST_DATABASE_URL` names the lane's own database in each lane's gate; `uv run pytest
tests/test_threat_model.py` passes after every merge.

## 2026-10-06 - architecture

### What happened
LP-08 made the hosted site's PHI mode default to `encrypt` by reading `VERCEL`. A check found
that a command run against the hosted database from anywhere else (a laptop, a scheduler) still
defaulted to `off` and could chain a plaintext input into it for good.

### Root cause
A protection was keyed to where the code runs, while the thing protected (the data) lives in the
database, which other processes reach too.

### Rule
Put a data protection where the data is. A setting that only the server sees protects nothing
from the other writers of the same database: mark the database itself (`settings`,
`phi_protected`) and have every writer check the mark before it starts.

### Verification
For each new protection, list every process that can write the store (server, CLI, scripts,
other deployments) and test the protection from one that does not share the server's settings.

## 2026-10-06 - testing

### What happened
LP-09's tests rotated the PHI key once and passed; the review found that every second rotation
by the documented steps was refused, and that the refusal's advice retired the key in use.

### Root cause
A guard meant for "a rotation left unfinished" was written against the first rotation only; no
test ran the operation twice, nor with the writes a live system makes between its steps.

### Rule
For any lifecycle operation (rotate, migrate, retire, re-key), test it run twice in a row, by the
documented steps, with the writes a live deployment makes between those steps.

### Verification
The test suite has a "second time" case for each such operation
(`test_rotations_follow_one_another_by_the_documented_steps`).

## 2026-10-07 - process

### What happened
A review-fix commit on #158 added a code comment naming real vendors and models; the PR's CI
failed `test_shipped.py::test_no_vendor_or_model_name_in_product_code_outside_the_providers`.
The full suite had run on the commit before the fixes, and only the files the fixes touched ran
after them.

### Root cause
Repository-wide checks (vendor names, SPDX headers, the manifest scan, the route inventory)
read every file, so a change anywhere can fail them; a targeted run over the changed modules'
own tests never includes them.

### Rule
After any commit that follows the last full run, run the repository-wide checks too:
`tests/test_shipped.py tests/test_spdx.py tests/test_supply_chain.py tests/test_licence_inventory.py`
(about 30 s), besides the changed modules' tests.

### Verification
Before pushing a review-fix commit, the command log shows those four files passing.

## 2026-10-08 - process

### What happened
LP-12g-iii (#186) wrote its new test module to `tests/test_isolation.py`, a path that already
held #151's tests that the suite reaches no live system the developer's shell names. The write
replaced that file; the review read the new file only, and the PR merged with those three tests
gone. Found while closing out LP-12, from a stale-looking reference in `tasks/todo.md`; restored
the same day, and the new module renamed `tests/test_two_organizations.py`.

### Root cause
A new file was written without checking the path was free, and the diff's "modified" status
for what was meant to be a new file was not read.

### Rule
Before writing a new file, check the path does not exist (`git ls-files <path>`, or `ls`). In a
PR that adds a module, `git diff --stat main...HEAD` must show it as new (`create mode`), never
as a rewrite of an existing one.

### Verification
The PR's diff stat (or `git diff --name-status main...HEAD`) lists each new module with `A`.

## 2026-10-08 - process

### What happened
Neon reached its 10-branch limit again despite the 2026-09-28 cleanup workflow, and new
previews (#209, #211, #213) got no database branch. Three branches had escaped it: GitHub never
started one PR's cleanup run (a billing failure, 10-06), a preview that finished building after
its 62-second merge recreated a deleted branch, and a branch pushed without a pull request never
had a close to trigger it. Meanwhile 24 cleanup runs were red only because the branch had never
been created (Dependabot's, or a preview refused at the limit), which hid the real failures.

### Root cause
The workflow deleted one branch, named by the pull request that just closed, once. Anything that
happened outside that one event was never looked at again, and "not found" counted as a failure.

### Rule
Clean up by sweeping the whole state, not by reacting to one event: the workflow
(`scripts/neon_preview_cleanup.py`) deletes every `preview/*` branch whose Git branch has no
open pull request and is gone or had one, on each close and every six hours. A branch already
gone is success.

### Verification
`gh run list --workflow neon-preview-cleanup.yml --limit 5` is green, and a dry run
(`gh workflow run neon-preview-cleanup.yml -f dry_run=true`) lists nothing to delete while
Neon holds only `main` and the branches of open pull requests.

## 2026-11-07 - testing

### What happened
The harder inbox benchmark's full suite passed (6,571 tests) while its new files were not yet
added to git. After the commit, `tests/test_shipped.py` failed: a Spanish frame's "llaman"
matched the model-name pattern (`llama`). The full run had not seen the new module at all.

### Root cause
Six test files list what they scan with `git ls-files` (`test_shipped`, `test_spdx`,
`test_public_tree`, `test_no_hidden_data_shipped`, `test_code_systems`, `test_x12_values`), so
they skip untracked files: a run before `git add` proves nothing about a new file.

### Rule
`git add` new files before the full run (staging is enough; the index is what `ls-files` reads).
If the full run went first, run those six files again once the new files are staged.

### Verification
`git status --short | grep '^??'` lists nothing of the change before the full run starts.

## 2026-11-10 - communication

### What happened
Asked to "examine the mendari model to make it more robust ... and capable to self improve", I
read "the Mendari model" as the naive Bayes classifier two packs ship, and measured that first.
The owner corrected it: Mendari is the engine the packs are built on.

### Root cause
The request named a thing no file, type or page of the repository names, and I took the nearest
match in the code without saying so before starting.

### Rule
When a request names something the repository does not, say in the first message what it is
taken to mean and what else it could mean, before measuring or planning, and start with the work
both readings share.

### Verification
The first message of the turn states the reading in one sentence.

## 2026-11-10 - syntax

### What happened
Writing `core/text.py` and its tests, each `\u` escape typed into the file-writing tool arrived in
the source as the character itself: curly quotes, a line separator and zero-width spaces sat in
string literals, some of them invisible. The tests passed all the same; ruff's ambiguous-character
rules (RUF001, RUF002) caught it.

### Root cause
The tool decodes `\u` escapes in the text it is given. `\x`, `\r` and `\N{...}` pass through.

### Rule
In source, write a character a reader cannot see, or cannot tell from an ASCII one, as
`\N{NAME}` (or build it with `chr()`), never as a `\u` escape typed into a file-writing tool. After
writing a file that handles such characters, list every non-ASCII character it holds and account
for each.

### Verification
A script over the changed files prints each character outside ASCII by its Unicode name, and the
list holds only intended, visible letters.

## 2026-11-10 - testing

### What happened
A new test of `mendari improve` meant to show that a run cannot be applied to a pack that has
changed. It called `apply(load_pack(INBOX_DIR), found)` with a run judged on a temporary copy of
that pack. The copy and the repository's own pack have the same digest, so nothing was refused:
the call appended 303 records to `packs/patient-inbox-router/data/` and rewrote its model. The
full suite was running in the background on the same tree, so its result was void. `git status`
showed it within a minute; the three files were restored with `git checkout`.

### Root cause
A test passed a path inside the repository to a function that writes. Nothing in the function
or the suite stood between a test and the repository's own packs.

### Rule
A function that writes into a pack refuses the bundled packs and the registry's cache by where
the pack is on disk (`core.registry.origin_of`), not by how it was loaded. A test of such a
function builds every path it passes from `tmp_path`, and its module has an autouse fixture that
fails a test after which the repository's pack no longer has its digest. While a full run is in
progress, nothing else that can write runs in the same tree.

### Verification
`git status --short packs datasets` is empty after every test run, and
`tests/test_improve.py` holds the fixture.

## 2026-11-10 - testing

### What happened
`mendari improve` held 30% of the new cases out and judged a retrained model on them. It told
cases apart by their whole input, so the same message with another time stamp, channel or
capitals was another case. A review gave it each message three times: copies fell on both sides
of the split and every held-out case came out right. It also learned from messages of the pack's
own test once their records differed in one field. The tests gave each message once.

### Root cause
"The same case" was decided by the record, while the model reads only part of it. Any field the
model does not read made two cases of one.

### Rule
When data is split to judge a model, identify a case by what the model reads of it (its
features), and use that one identity everywhere: for duplicates, for conflicting labels, for
what the pack already holds, and for the split itself. Test the split with every case given
again as another record of the same text.

### Verification
`tests/test_improve.py::test_a_message_received_again_is_one_case` gives each message three
times and expects the run on each message once.

## 2026-11-25 - communication

### What happened
Rewriting the root files for wave 6, the README said in three places that the repository was not
public (`gh repo view` says it is), and the README, CLAUDE.md and the `core` marker in
pyproject.toml said the edit loop takes about 20 s (measured: 54 s and 60 s, 48 s of it CPU).
No test failed for any of them, and the same files' claims about the code were held by tests.

### Root cause
Facts about what surrounds the code (the repository's visibility, a package index, a hosted
site, a tag, how long a command runs) were written once and then carried forward as if a test
held them, like the facts about the code.

### Rule
Whenever a root file is edited, check each sentence it states about something outside the code
at its source: the repository's visibility (`gh repo view --json visibility`), the package index
(`https://pypi.org/pypi/mendari/json`), a hosted site (its address), the remote's tags
(`git ls-remote --tags`), a time or a count (run it and measure). A number no test pins says
how it was measured, or is left out.

### Verification
The commit that edits a root file names each such fact and the check behind it, as the wave 6
phase 5A commits do.
