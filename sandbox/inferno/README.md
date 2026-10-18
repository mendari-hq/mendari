# Inferno against Mendari

The Inferno test kits play the other side of a Da Vinci exchange against a running engine, on
synthetic data only. Today one suite runs: the **Da Vinci DTR test kit v0.18.0** (commit
`1b76dd3f565433a3157808da2d6d0c08a797b2f0`), suite `dtr_payer_server_v220`, in which Inferno is
the DTR client and calls Mendari's `POST /fhir/Questionnaire/$questionnaire-package`. Mendari's
DTR route is the payer side (DTR's "DTR Server"), so a DTR client is its counterpart.

The workflow `conformance.yml` runs it weekly (Tuesdays) and by hand. It is not a required
check: a run builds the kit, and its validator downloads and loads every implementation guide
the suite needs from packages.fhir.org, which takes minutes, and fails when that site does. The
offline `fhir-conformance` job in `ci.yml` stays the required gate for the resources Mendari
produces.

## What a run proves, and what it cannot

It proves how the operation behaves for a DTR client, test by test: the package for an order, a
`context` or a questionnaire canonical; the pre-answered QuestionnaireResponse and its
references; the warning for a questionnaire not found (DTR oper-9, test 3.6.01); the `4xx`
OperationOutcome for source data that names no decision (spec-130, test 3.6.02); and the gaps
below, each by its requirement id.

It cannot prove:
- that a payer accepts Mendari's PAS request or its documentation;
- that an EHR's DTR client renders and completes the package;
- that a pack's requirement tables match a real payer's policy (the payers and codes here are
  fictional);
- X12 or CPT correctness (both are licensed, and neither can be validated here);
- timing, or DTR's security and privacy requirements.

The kit itself is marked draft: passing it does not prove conformance. Group 2 (SMART Backend
Services) is left out until LP-23, whose CRD suite (its PR 23l) reuses this harness.

The HL7-DaVinci/dtr reference app is never run. Given a questionnaire canonical, it builds the
operation's URL from the canonical's origin, so it would POST to https://mendari.co. It also
reads the package under DTR 2.0.1's parameter name and would not see a 2.2.0 package.

## The expected results

`dtr_payer_server_v220_expected.json` lists every test of groups 1, 3 and 4 by its Inferno id,
with its short id, the result it gives today and the requirement ids the kit says it verifies.
Each result that is not a pass says why. `scripts/inferno_check.py` gives the job its verdict:
any difference fails, a new failure and an unexpected pass alike, so the known gaps change only
by a pull request that records the change.

Known gaps, each with its reason in the file:

| Test | Requirement | Gap |
|---|---|---|
| 1.01 | conf-1 | The CapabilityStatement claims `$questionnaire-package` only |
| 3.4.01 | spec-17 | No question depends on another's answer: no `enableWhen` |
| 3.4.04 | spec-54 | Answers are filled on the server, with no CQL for the EHR to run |
| 3.5.01 (skip) | oper-5 | No ValueSet is returned, so nothing is expanded |
| 3.6.03 | spec-147 | No `$next-question` (no adaptive forms) |
| 3.7.02 to 3.7.04 | conf-2 | Must-support elements the questionnaires do not use |
| 4.01 | oper-1 | No `$log-questionnaire-errors` |

Tests that do not apply (no adaptive form, no CQL, no ValueSet, no Binary) are `omit`, Inferno's
own result for them, with the reason too.

## What leaves the machine

- Building: the base images (by digest), the kit's source (from GitHub, by commit), Bundler and
  the gems (from rubygems.org, each checked against its sha256 in `Gemfile.lock`).
- Running: the validator downloads the implementation guides it loads from packages.fhir.org.
  Nothing else: every validator in the kit runs without a terminology server
  (`offline_terminology.rb`; the validator's log says "No Terminology Server"), so no resource
  goes to tx.fhir.org. Inferno's own host is the internal nginx, and the ONC reference server
  it would use for other suites is a dead local URL.
- No secret is used. The engine runs open (no sign-in) with PHI mode `off`, and ignores the
  static token Inferno sends.

The raw output can quote request and response bodies. The workflow keeps it as an artifact for
7 days and never commits it.

## Running it locally

From the repository root, with Docker (check `docker ps` first: the engine is published on
`127.0.0.1:8090` and the web app on `127.0.0.1:4580`; `MENDARI_INFERNO_ENGINE_PORT` and
`MENDARI_INFERNO_UI_PORT` move them):

```bash
docker compose -f sandbox/inferno/docker-compose.yml build engine inferno
docker compose -f sandbox/inferno/docker-compose.yml up -d --wait engine
uv run python scripts/inferno_dtr_inputs.py --engine http://127.0.0.1:8090 \
  --out var/inferno/dtr_payer_server_v220_preset.json
docker compose -f sandbox/inferno/docker-compose.yml run --rm -T --no-deps inferno \
  bundle exec ruby /opt/mendari/list_tests.rb dtr_payer_server_v220 1 3 4 \
  > var/inferno/dtr_payer_server_v220_tests.json
docker compose -f sandbox/inferno/docker-compose.yml run --rm -T inferno \
  bundle exec inferno execute --suite dtr_payer_server_v220 --groups 1 3 4 \
  --preset-file /inputs/dtr_payer_server_v220_preset.json --outputter json \
  > var/inferno/dtr_payer_server_v220_results.json
uv run python scripts/inferno_check.py var/inferno/dtr_payer_server_v220_results.json \
  --expected sandbox/inferno/dtr_payer_server_v220_expected.json \
  --tests var/inferno/dtr_payer_server_v220_tests.json
docker compose -f sandbox/inferno/docker-compose.yml down --volumes
```

The validator's Java heap may grow to most of the memory Docker has, and each run starts a
validator session of its own, holding every guide it loaded. With 8 GB for Docker, a second run
against the same validator ran out of memory: remove the validator between runs
(`docker compose -f sandbox/inferno/docker-compose.yml rm -sf hl7_validator_service`).

`docker compose -f sandbox/inferno/docker-compose.yml up -d` also starts the web app, at
http://127.0.0.1:4580, for looking at a session by hand.

## Bumping the kit

The kit's images and gems are pinned here, not by Dependabot: a bump can change the results.

1. Set `KIT_COMMIT` in the `Dockerfile` to the new release's commit.
2. Regenerate `Gemfile.lock` from the kit's own, in the pinned Ruby image, with the pinned
   Bundler: `bundle lock --add-platform aarch64-linux-gnu`, then `bundle lock --remove-platform`
   for each platform that is not Linux, then `bundle lock --add-checksums`.
3. Build and run, then refresh the expected results from the run with `--write` (each reason
   whose result did not change is kept), and write a reason for each new result that is not a
   pass. Review the differences like any other change in behaviour.

## Files

| File | What it is |
|---|---|
| `Dockerfile` | The kit, built from its commit with Mendari's lockfile |
| `Gemfile.lock` | The kit's gems for Linux, each with its sha256 |
| `offline_terminology.rb` | Every validator in the kit without a terminology server |
| `list_tests.rb` | A run's tests, with their short ids and requirement ids |
| `docker-compose.yml` | The engine, the kit, its worker, redis, the validator, FHIRPath and nginx |
| `nginx.conf` | The web app's front, on this machine only |
| `dtr_payer_server_v220_expected.json` | The expected results |
