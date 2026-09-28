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
