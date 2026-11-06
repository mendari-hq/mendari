# Inferno against Mendari

The Inferno test kits play the other side of an exchange against a running engine, on synthetic
data only. Four suites run, each a job of the workflow `conformance.yml`, from two images:

| Job | Suite | Kit | Inferno is | Image |
|---|---|---|---|---|
| `dtr-payer` | `dtr_payer_server_v220` | Da Vinci DTR v0.18.0 | the DTR client | `Dockerfile` |
| `crd-server` | `crd_server_v221` | Da Vinci CRD v0.15.0 | the EHR's CDS client | `scripted/` |
| `smart-client` | `smart_client_stu2_2` | SMART App Launch v1.0.3 | the EHR of a launch | `scripted/` |
| `bulk-client` | `bulk_data_v200_client` | Bulk Data v0.14.0 | the export's server | `scripted/` |

The DTR suite is below; the other three are [The scripted suites](#the-scripted-suites).

The workflow runs weekly (Tuesdays) and by hand, never on a pull request. It is not a required
check: a run builds the kits, and the validator downloads and loads every implementation guide
a suite needs from packages.fhir.org, which takes minutes, and fails when that site does. The
offline `fhir-conformance` job in `ci.yml` stays the required gate for the resources Mendari
produces.

## The DTR suite

The **Da Vinci DTR test kit v0.18.0** (commit `1b76dd3f565433a3157808da2d6d0c08a797b2f0`),
suite `dtr_payer_server_v220`, in which Inferno is the DTR client and calls Mendari's
`POST /fhir/Questionnaire/$questionnaire-package`. Mendari's DTR route is the payer side (DTR's
"DTR Server"), so a DTR client is its counterpart.

### What a run proves, and what it cannot

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
Services) is left out until LP-23.

The HL7-DaVinci/dtr reference app is never run. Given a questionnaire canonical, it builds the
operation's URL from the canonical's origin, so it would POST to https://mendari.co. It also
looks for the package under the parameter name `PackageBundle`, which neither DTR version uses
(2.0.1 names it `return`, 2.2.0 `packagebundle`), so it would not see Mendari's package.

### The expected results

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

### What leaves the machine

- Building: the base images (by digest), the kit's source (from GitHub, by commit), Bundler
  (from rubygems.org, checked against `BUNDLER_SHA256` in the `Dockerfile`) and the gems (from
  rubygems.org, each checked against its sha256 in `Gemfile.lock`).
- Running: the validator downloads the implementation guides it loads from packages.fhir.org.
  Nothing else: every validator in the kit runs without a terminology server
  (`offline_terminology.rb`; the validator's log says "No Terminology Server"), so no resource
  goes to tx.fhir.org. Inferno's own host is the internal nginx, and the ONC reference server
  it would use for other suites is a dead local URL.
- No secret is used. The engine runs open (no sign-in) with PHI mode `off`, and ignores the
  static token Inferno sends.

The raw output can quote request and response bodies. The workflow keeps it as an artifact for
7 days and never commits it.

### Running it locally

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

## The scripted suites

The CRD, SMART App Launch and Bulk Data suites wait for calls: a hook's answer, a launch, an
export. `inferno execute` runs a suite in one process with no web server, so it stops at the
first such test. These three run instead through Inferno's web app and its worker, sharing one
database, in a compose project of their own (`scripted/docker-compose.yml`, standalone: the
root `docker-compose.yml` and this one both have an `engine`, which an overlay would merge).
`scripts/inferno_session.py` works each session through the web app's REST API, as a person at
its pages would: it starts each group with the inputs `scripts/inferno_scripted_inputs.py`
wrote, and when a test waits for the client it does the client's part, once, in the engine's
container (`docker compose exec`), then calls the test's resume URL. It writes the results as
`inferno execute --outputter json` does, and `scripts/inferno_check.py` gives the verdict, as
for DTR. Nobody clicks anything: Inferno's SMART authorization endpoint answers at once, with no
sign-in or consent page.

The image (`scripted/Dockerfile`) is the Da Vinci CRD kit v0.15.0 at commit
`86be7f4bf6817960aa204f8ce134cea466bd73d2`, with the Bulk Data kit v0.14.0 added to its Gemfile
(`gem 'bulk_data_test_kit', '0.14.0'`; `lib/mendari_kits.rb` loads it), on inferno_core 1.6.0.
The SMART App Launch kit v1.0.3 comes in as a dependency. Every gem is in `scripted/Gemfile.lock`
with its sha256, the CRD kit (fetched by commit) excepted. The DTR image is separate and
unchanged.

### CRD: Inferno as the EHR's CDS client (`crd-server`)

The signed-in engine (`engine-signin`: sign-in on, PHI mode `encrypt` with keys made for the
run) registers Inferno as an EHR is registered (`MENDARI_CDS_CLIENTS`,
[connecting an EHR](../../docs/guides/connecting-an-ehr.md#conformance-runs)): its `iss`
`http://nginx/custom/crd_server_v221` (Inferno's host, `INFERNO_HOST`, plus its suite's path),
its keys from that plus `/jwks.json`, and that plus `/fhir` as the one FHIR base it sends. Its
FHIR allow-list is `http://nginx` alone. Inferno signs each call (ES384) and Mendari verifies it
as any EHR's: the run proves the registry and the verifier against a client Mendari did not
write. The kit's private keys are in its public repository, so this registration belongs in
this sandbox only.

Groups 1 (discovery, with authentication), 2 (one hook call), 3.4 (`order-sign`) and 4 (the
specific coverage answers) run. The requests are the vendor-shaped EHR profile's five
prior-authorization patients (`sandbox/ehr_profile.py`), each `order-sign` as an EHR sends it:
the draft orders, no prefetch, and the FHIR authorization Inferno completes with its own token,
so the engine reads each record from Inferno's Mock EHR, which holds those patients. Group 4's
Technical Issues call is the first patient's order with its Patient and Coverage prefetched, as
the kit's own example sends them: Inferno's FHIR server then fails every read (a `500` to the
token that call carries), and the engine abstains with `indeterminate` coverage for a
`technical` reason. Group 4 also adds three patients made from the first: no member id, no
Coverage, a cancelled Coverage. The trial, referral and inbox services are on the discovery
ignore list: theirs are not CRD hooks.

Not run:
- Groups 3.1 to 3.3, 3.5 and 3.6: hooks Mendari does not serve (`appointment-book`,
  `encounter-start`, `order-select`, `order-dispatch`, `encounter-discharge`).
- Group 3.7 (Cross-Invocation Response Validation). Its test 3.7.02 (Coverage Information
  must-support elements) names no requirement id, and fails: it asks the run's answers, taken
  together, to show every must-support part of CRD's coverage-information extension
  (`billingCode`, `contact`, `dependency`, `detail`, `doc-purpose`, `expiry-date`,
  `info-needed`, `questionnaire`, `satisfied-pa-id` and more), most of which no pack has data
  for. An expected failure must name the requirement it leaves unmet (`inferno_check.py`), so
  the group is not started rather than the rule loosened. In the run that found this, 3.7.01,
  3.7.03, 3.7.04 and 3.7.06 passed, and 3.7.05 failed (US Core 3.1.1 support not demonstrated).

Known gaps (`crd_server_v221_expected.json` has each reason):

| Test | Requirement | Gap |
|---|---|---|
| 1.01 | sec-2 | Plain HTTP in the sandbox: TLS belongs to the deployment |
| 3.4.3.01, 4.1.3.01 | resp-4, resp-5, resp-13 | Each card's `mendari.decision_id` extension, which CRD's response model does not define |
| 3.4.3.02 | resp-19, resp-22 | The audit-trail link card also carries a suggestion, a Task with no id |
| 3.4.3.06 | hook-16, resp-28, hook-39, resp-29 | Four patients are `not_checked` (the site's own payer id), an outcome with no coverage information |
| 3.4.3.03, .04, .08 to .11 (skip) | resp-24, resp-72, ... | Card and action types Mendari does not send |
| 4.2.3.02, 4.2.3.03, 4.4.3.02, 4.4.3.03 (skip) | resp-26 ..., resp-44, resp-45 | No member and no active-coverage checks: those orders are `not_checked`, with no coverage information |
| 4.3.3.02, 4.3.3.03 (skip) | resp-26 ..., resp-45 | No Coverage, so no coverage information |

Since LP-23k the prior-authorization pack reads the profile's linked notes (a text/plain or
text/html Binary, read from the Mock EHR), and follows the Coverage's payor to its Organization.
That Organization holds the site's own payer id, which the pack does not know and no site code
map translates (how a payer field reads one is the owner's open question), so every patient whose
record reads is decided `not_checked`, which shows no card and declares no coverage information:
2.3.01, 4.2.3.01, 4.3.3.01 and 4.4.3.01 pass, since no card is left to carry the extension, and
the group 4 coverage-information tests skip with nothing to check. Only ehr-pa-3, whose note is
RTF, abstains, with `indeterminate` coverage for a `technical` reason, and so does the Technical
Issues call. 23k's PR refreshed the expected results from two local runs (2026-11-06), with the
same results; before, most decisions abstained on their unread linked note.

Test 1.06 (standard prefetch expressions) passes with warnings, which `inferno_check.py` does not
compare: the prior-authorization pack's prefetch asks for `Condition`, `Procedure` and
`DocumentReference` searches that are not among CRD's standard expressions (plan decision 53).

### SMART App Launch: Inferno as the EHR (`smart-client`)

Suite option `client_type`: `SMART,authorization_code,public`, Mendari's EHR launch as a public
client. The open engine (`engine`, PHI mode `off`, one pack, so a launch needs no `pack`) is
registered with Inferno by its client id, launch URL and redirect URI. When the access test
waits, the driver runs `mendari sandbox smart-launch --launch-url <Inferno's launch URL>` in the
engine's container: the browser's part (the engine's launch, Inferno's authorize endpoint, the
engine's callback, the cookies carried by hand because they are `Secure`), then the patient in
context read through the engine (`/api/smart/patient`, a FHIR read with the launch's token), and
logging out. Tests 5.03 (the launch), 5.07 (the authorization request: `aud`, PKCE S256, `state`,
`launch`), 5.10 (the token request) and 5.12 (the token's use) pass.

| Test | Requirement | Gap |
|---|---|---|
| 3.01 | 20, 21 | The launch URL and redirect URI are plain http in the sandbox; the kit asks https |

The sign-in profile (scopes `openid fhirUser`, which the engine asks for with sign-in on) is not
run: Inferno's SMART configuration publishes no `jwks_uri`, and its id_token has no `nonce`, and
Mendari's sign-in refuses both (`standards/oidc.py`), as it should. So the engine here asks for
`launch patient/*.rs` (`MENDARI_SMART_SCOPES`), and the signed-in launch is proven against
smart-launcher-2 in `ci.yml`'s `sandbox-e2e`, and at a site against its EHR.

### Bulk Data: Inferno as the export's server (`bulk-client`)

Inferno serves the export and passes it on to this project's HAPI (`FHIR_REFERENCE_SERVER`,
seeded with synthetic referral patients). The driver makes an EC P-384 key for the run: its
public half registers the client (`smart_jwk_set`), its private half reaches `mendari bulk
export` in the engine through the environment only. The export signs in with SMART Backend
Services, polls (Inferno asks for 120 s between polls, so a run takes two minutes or more),
downloads every file, then sends the DELETE Bulk Data STU2 says a client may send to the status
URL once it has the files. A DELETE the server refuses or does not answer is reported
(`delete_refused`, `delete_timeout`, `delete_unreachable`) and never fails or changes the
export. All 8 tests pass; test 1.01 warns that a JWK Set given by value, not by URL, is
discouraged.

### What leaves the machine

- Building: the base images (by digest), the CRD kit's source (from GitHub, by commit), Bundler
  (from rubygems.org, by sha256) and the gems (from rubygems.org, each by its sha256).
- Running: the validator downloads the implementation guides it loads from packages.fhir.org
  (the CRD suite's: CRD 2.2.1 and US Core). Nothing else: every validator in the image runs
  without a terminology server (`scripted/offline_terminology.rb`, which requires every other
  file in the kit's `lib/` first, whichever kits the image carries; the SMART and Bulk client
  suites define no validator). Every URL Inferno makes is on the internal nginx, the Bulk
  suite's server is this project's HAPI, and the engines reach only `http://nginx`.
- No secret is used: the signed-in engine's PHI keys are made for the run in the shell (the job
  masks them), the Bulk client's key is made by the driver and dies with it, and the kit's own
  CRD signing keys are public.

### Running them locally

From the repository root, with Docker; one suite per `up`, and `down --volumes` after each (check
`docker ps` first: nginx is published on `127.0.0.1:4580`, the open engine on `127.0.0.1:8090`
and the signed-in one on `127.0.0.1:8091`; `MENDARI_INFERNO_UI_PORT`,
`MENDARI_INFERNO_ENGINE_PORT` and `MENDARI_INFERNO_SIGNIN_PORT` move them; never run this
project and the DTR one at once, they share those ports).

```bash
F=sandbox/inferno/scripted/docker-compose.yml
docker compose -f $F build engine inferno

# CRD
export MENDARI_PHI_KEY=$(python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())")
export MENDARI_INPUT_HASH_KEY=$(python3 -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())")
docker compose -f $F up -d --wait engine-signin nginx
uv run python scripts/inferno_scripted_inputs.py crd --out var/inferno/crd_server_v221_inputs.json
uv run python scripts/inferno_session.py crd_server_v221 \
  --inputs var/inferno/crd_server_v221_inputs.json --groups 1 2 3.4 4 \
  --out var/inferno/crd_server_v221_results.json --tests var/inferno/crd_server_v221_tests.json
uv run python scripts/inferno_check.py var/inferno/crd_server_v221_results.json \
  --expected sandbox/inferno/crd_server_v221_expected.json \
  --tests var/inferno/crd_server_v221_tests.json
docker compose -f $F down --volumes

# SMART App Launch (public client)
docker compose -f $F up -d --wait engine nginx
uv run python scripts/inferno_scripted_inputs.py smart --out var/inferno/smart_client_stu2_2_inputs.json
uv run python scripts/inferno_session.py smart_client_stu2_2 \
  --inputs var/inferno/smart_client_stu2_2_inputs.json \
  --option client_type=SMART,authorization_code,public \
  --out var/inferno/smart_client_stu2_2_results.json --tests var/inferno/smart_client_stu2_2_tests.json
uv run python scripts/inferno_check.py var/inferno/smart_client_stu2_2_results.json \
  --expected sandbox/inferno/smart_client_stu2_2_expected.json \
  --tests var/inferno/smart_client_stu2_2_tests.json
docker compose -f $F down --volumes

# Bulk Data
docker compose -f $F --profile bulk up -d --wait engine fhir nginx
docker compose -f $F --profile bulk exec -T engine mendari sandbox seed \
  --fhir http://fhir:8080/fhir --pack mendari/referral-router --count 10 --wait 300
uv run python scripts/inferno_scripted_inputs.py bulk --out var/inferno/bulk_data_v200_client_inputs.json
uv run python scripts/inferno_session.py bulk_data_v200_client \
  --inputs var/inferno/bulk_data_v200_client_inputs.json \
  --out var/inferno/bulk_data_v200_client_results.json \
  --tests var/inferno/bulk_data_v200_client_tests.json
uv run python scripts/inferno_check.py var/inferno/bulk_data_v200_client_results.json \
  --expected sandbox/inferno/bulk_data_v200_client_expected.json \
  --tests var/inferno/bulk_data_v200_client_tests.json
docker compose -f $F --profile bulk down --volumes
```

The CRD run takes about five minutes once the validator has loaded its guides, the SMART run
under a minute, and the Bulk run a little over two.

**Memory.** Each JVM's heap is capped (`JAVA_TOOL_OPTIONS`, which the validator, the FHIRPath
service and HAPI all honour): the validator at 3 GB, FHIRPath at 512 MB, HAPI at 1 GB. The web
app and its worker each take about 0.4 to 0.6 GB, as each loads every suite in the image. A CRD
run peaked at about 5 GB in all on a machine with 8 GB for Docker (the validator about 3.5 GB),
and the Bulk run at about 2.7 GB. nginx resolves every service it proxies to as it starts, so
the validator runs for the SMART and Bulk suites too, idle and small. If a run is short of
memory, remove the validator between runs (`docker compose -f $F rm -sf hl7_validator_service`).

The web app is at http://127.0.0.1:4580 while a run is up, for looking at its session by hand.

## Bumping the kit

The kits' images and gems are pinned here, not by Dependabot: a bump can change the results.

1. Set `KIT_COMMIT` in the `Dockerfile` (DTR) or `scripted/Dockerfile` (CRD) to the new
   release's commit; for the Bulk Data kit, its version in `scripted/Gemfile`.
2. Regenerate that image's `Gemfile.lock` from the kit's own (for `scripted/`, with the Gemfile
   here), in the pinned Ruby image, with the pinned Bundler: `bundle lock`, then
   `bundle lock --add-platform aarch64-linux-gnu`, then `bundle lock --remove-platform` for each
   platform that is not Linux, then `bundle lock --add-checksums`. The lock holds no checksum for
   Bundler itself: if it is `BUNDLED WITH` a new version, set `BUNDLER_VERSION` and
   `BUNDLER_SHA256` in the Dockerfile to that version and the sha256 rubygems.org lists for it.
3. Build and run, then refresh the expected results from the run with `--write` (each reason
   whose result did not change is kept), and write a reason for each new result that is not a
   pass, with the version in the file's header. Review the differences like any other change in
   behaviour.

## Files

| File | What it is |
|---|---|
| `Dockerfile` | The DTR kit, built from its commit with Mendari's lockfile |
| `Gemfile.lock` | The DTR kit's gems for Linux, each with its sha256 |
| `offline_terminology.rb` | Every validator in the DTR kit without a terminology server |
| `list_tests.rb` | A DTR run's tests, with their short ids and requirement ids |
| `docker-compose.yml` | DTR's: the engine, the kit, its worker, redis, the validator, FHIRPath and nginx |
| `nginx.conf` | Inferno's front, for both projects: `INFERNO_HOST`, on this machine only |
| `dtr_payer_server_v220_expected.json` | DTR's expected results |
| `scripted/Dockerfile` | The CRD kit from its commit, with the Bulk Data kit, from Mendari's lock |
| `scripted/Gemfile`, `scripted/Gemfile.lock` | The CRD kit's Gemfile with the Bulk Data kit; its gems for Linux, each with its sha256 |
| `scripted/lib/mendari_kits.rb` | Loads the Bulk Data kit's suites |
| `scripted/offline_terminology.rb` | Every validator in the image without a terminology server |
| `scripted/docker-compose.yml` | The two engines, the web app and its worker on one database, redis, the validator, FHIRPath, HAPI (profile `bulk`) and nginx |
| `crd_server_v221_expected.json`, `smart_client_stu2_2_expected.json`, `bulk_data_v200_client_expected.json` | The scripted suites' expected results |

The driver and the inputs are `scripts/inferno_session.py` and `scripts/inferno_scripted_inputs.py`
(DTR's inputs: `scripts/inferno_dtr_inputs.py`).
