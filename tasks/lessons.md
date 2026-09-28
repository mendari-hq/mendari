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
