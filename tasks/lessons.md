# Lessons

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
`openhealth eval ... --dataset test` prints `Critical urgency=urgent  N/N detected, 0 automated`.

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
A local test Postgres for OpenHealth was started on port 55432, which another project's
database container already owned. The new container never got the port, so the first test run
connected to the other project's database. Authentication failed, so nothing was read or
written, but the tests were pointed at the wrong system.

### Root cause
Assumed a `docker run` succeeded (its output was discarded) and assumed a port was free.

### Rule
OpenHealth never shares resources with other projects. Before using a local port, check which
container owns it (`docker ps`, `lsof -iTCP:<port>`). After starting a container, verify it
with `docker port <name>` and by querying `current_database()` before running tests.
OpenHealth's test database is `openhealth_test` on 127.0.0.1:56432.

### Verification
`docker port openhealth-pg-test` shows `127.0.0.1:56432`, and
`select current_database()` returns `openhealth_test`.

## 2026-09-28 - process

### What happened
Before deploying I told the user that Vercel Authentication protected all deployments. The
project setting was `all_except_custom_domains`, which exempts the production domain, so after
the first production deploy `openhealth-hub-gray.vercel.app` served the unauthenticated app
publicly for a few minutes. Audit and Jev usage showed no traffic from outside.

### Root cause
I read the setting's name as "everything" without checking what counts as a "custom domain",
and I never tested an unauthenticated request against production.

### Rule
Verify access control empirically, not from setting names: after every deploy, request each
production and preview URL without credentials and expect a redirect to login. The project
must use `ssoProtection.deploymentType = "all"` while the app has no authentication of its own.

### Verification
`curl -s -o /dev/null -w "%{http_code}" https://openhealth-hub-gray.vercel.app/api/health`
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
`gh api repos/Junebugg1214/openhealth-hub/branches/main/protection` lists the five checks,
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
`openhealth eval <pack> --dataset red_flags_holdout --provider safety_screen=rules_and_jev`
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
PR 5 had auto-merge on. While its CI ran, I changed `OPENHEALTH_PROVIDERS` on Vercel and pushed
a follow-up commit. The PR merged from its first commit 5 seconds before the setting changed:
production kept the old notes reader, and the follow-up commit never reached `main`.

### Root cause
Auto-merge fires the moment checks pass, so anything done "before it merges" is a race.
Vercel reads environment variables when a deployment is built, not at runtime.

### Rule
Finish every change a PR depends on (settings, follow-up commits) before enabling auto-merge.
After changing a Vercel environment variable, redeploy (`vercel redeploy <url> --target
production`) and confirm the setting in `/api/health` before calling it done.

### Verification
`/api/health` shows the expected providers for every pack, and `git log origin/main` contains
every commit of the PR branch.

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
`openhealth validate` now checks every behaviour-test input against the input schema (except
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
Lifting the Jev daily limit, I patched Vercel env entry `hXL1behAMcHlzzTS` to `none` from
memory. That entry was production's `OPENHEALTH_PROVIDERS`, not the Jev limit. The running
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
over `src/openhealth/dashboard/*.js`, reporting unresolved names (TS2304) and unused imports
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
existed. It did, with deployment guidance (Vercel `ssoProtection: all`, remote providers, pack
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
In CI, start background servers from the venv (`.venv/bin/openhealth …`, after `uv sync`), never
with `uv run`.

### Verification
`grep -n 'uv run .*&$' .github/workflows/*.yml` finds nothing.
