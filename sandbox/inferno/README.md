# Inferno against Mendari

The Inferno test kits play the other side of an exchange against a running engine, on synthetic
data only. Two suites run, each a job of the workflow `conformance.yml`, from one image
(`scripted/`):

| Job | Suite | Kit | Inferno is |
|---|---|---|---|
| `smart-client` | `smart_client_stu2_2` | SMART App Launch v1.0.3 | the EHR of a launch |
| `bulk-client` | `bulk_data_v200_client` | Bulk Data v0.14.0 | the export's server |

The workflow runs weekly (Tuesdays) and by hand, never on a pull request. It is not a required
check: a run builds the kits, which takes minutes. The offline `fhir-conformance` job in
`ci.yml` stays the required gate for the resources Mendari produces.

## The scripted suites

Both suites wait for calls: a launch, an export. `inferno execute` runs a suite in one process
with no web server, so it stops at the first such test. These run instead through Inferno's web
app and its worker, sharing one database, in a compose project of their own
(`scripted/docker-compose.yml`, standalone: the root `docker-compose.yml` and this one both have
an `engine`, which an overlay would merge). `scripts/inferno_session.py` works each session
through the web app's REST API, as a person at its pages would: it starts each group with the
inputs `scripts/inferno_scripted_inputs.py` wrote, and when a test waits for the client it does
the client's part, once, in the engine's container (`docker compose exec`), then calls the
test's resume URL. It writes the results as `inferno execute --outputter json` does. Nobody
clicks anything: Inferno's SMART authorization endpoint answers at once, with no sign-in or
consent page.

The engine runs open (no sign-in), PHI mode `off`, and serves the bundled demo agent alone: the
image ships it, and nothing from the test fixtures is mounted.

The image (`scripted/Dockerfile`) is the Da Vinci CRD kit v0.15.0 at commit
`86be7f4bf6817960aa204f8ce134cea466bd73d2`, with the Bulk Data kit v0.14.0 added to its Gemfile
(`gem 'bulk_data_test_kit', '0.14.0'`; `lib/mendari_kits.rb` loads it), on inferno_core 1.6.0.
The SMART App Launch kit v1.0.3 comes in as a dependency of the CRD kit. The CRD kit's own suite
is in the image too, and no job runs it. Every gem is in `scripted/Gemfile.lock` with its
sha256, the CRD kit (fetched by commit) excepted.

### The expected results

`<suite>_expected.json` lists every test a run executes by its Inferno id, with its short id,
the result it gives today and the requirement ids the kit says it verifies. Each result that is
not a pass says why. `scripts/inferno_check.py` gives each job its verdict: any difference
fails, a new failure and an unexpected pass alike, so the known gaps change only by a pull
request that records the change.

### SMART App Launch: Inferno as the EHR (`smart-client`)

Suite option `client_type`: `SMART,authorization_code,public`, Mendari's EHR launch as a public
client. The engine (one agent, so a launch needs no `agent`) is registered with Inferno by its
client id, launch URL and redirect URI, and Inferno's FHIR server is given one synthetic
patient, made as the sandbox makes its own, as the launch's context. When the access test
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
seeded with the bundled demo agent's synthetic patients and their portal messages). The driver
makes an EC P-384 key for the run: its
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
- Running: nothing. The SMART and Bulk client suites define no validator, and every validator in
  the image runs without a terminology server (`scripted/offline_terminology.rb`, which requires
  every other file in the kit's `lib/` first, whichever kits the image carries), so no resource
  goes to tx.fhir.org. Every URL Inferno makes is on the internal nginx, the Bulk suite's server
  is this project's HAPI, and the engine reaches only `http://nginx`.
- No secret is used: the engine runs open with PHI mode `off`, and the Bulk client's key is made
  by the driver and dies with it.

The raw output can quote request and response bodies. The workflow keeps it as an artifact for
7 days and never commits it.

### Running them locally

From the repository root, with Docker; one suite per `up`, and `down --volumes` after each (check
`docker ps` first: nginx is published on `127.0.0.1:4580` and the engine on `127.0.0.1:8090`;
`MENDARI_INFERNO_UI_PORT` and `MENDARI_INFERNO_ENGINE_PORT` move them).

```bash
F=sandbox/inferno/scripted/docker-compose.yml
docker compose -f $F build engine inferno

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
  --fhir http://fhir:8080/fhir --count 10 --wait 300
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

The SMART run takes under a minute, and the Bulk run a little over two.

**Memory.** Each JVM's heap is capped (`JAVA_TOOL_OPTIONS`, which the validator, the FHIRPath
service and HAPI all honour): the validator at 3 GB, FHIRPath at 512 MB, HAPI at 1 GB. The web
app and its worker each take about 0.4 to 0.6 GB, as each loads every suite in the image. The
Bulk run peaked at about 2.7 GB. nginx resolves every service it proxies to as it starts, so
the validator runs for both suites, idle and small. If a run is short of memory, remove the
validator between runs (`docker compose -f $F rm -sf hl7_validator_service`).

The web app is at http://127.0.0.1:4580 while a run is up, for looking at its session by hand.

## Bumping the kit

The kits' images and gems are pinned here, not by Dependabot: a bump can change the results.

1. Set `KIT_COMMIT` in `scripted/Dockerfile` to the CRD kit's new release's commit; for the
   Bulk Data kit, its version in `scripted/Gemfile`.
2. Regenerate `scripted/Gemfile.lock` from the kit's own, with the Gemfile here, in the pinned
   Ruby image, with the pinned Bundler: `bundle lock`, then
   `bundle lock --add-platform aarch64-linux-gnu`, then `bundle lock --remove-platform` for each
   platform that is not Linux, then `bundle lock --add-checksums`. The lock holds no checksum for
   Bundler itself: if it is `BUNDLED WITH` a new version, set `BUNDLER_VERSION` and
   `BUNDLER_SHA256` in the Dockerfile to that version and the sha256 rubygems.org lists for it.
   `inferno_core` is taken apart: the Dockerfile fetches its release by sha256 and moves its
   `activesupport` and `sqlite3` pins to their fixed releases (1.6.0 pins both to ranges with
   open advisories), and the Gemfile names it by path. When a release of `inferno_core` takes
   the fixed versions, set it in the Gemfile as any gem and drop that step.
3. Build and run, then refresh the expected results from the run with `--write` (each reason
   whose result did not change is kept), and write a reason for each new result that is not a
   pass, with the version in the file's header. Review the differences like any other change in
   behaviour.

## Files

| File | What it is |
|---|---|
| `nginx.conf` | Inferno's front: `INFERNO_HOST`, on this machine only |
| `scripted/Dockerfile` | The CRD kit from its commit, with the Bulk Data kit, from Mendari's lock; `inferno_core` by sha256, with two pins moved to fixed releases |
| `scripted/Gemfile`, `scripted/Gemfile.lock` | The CRD kit's Gemfile with the Bulk Data kit; its gems for Linux, each with its sha256 |
| `scripted/lib/mendari_kits.rb` | Loads the Bulk Data kit's suites |
| `scripted/offline_terminology.rb` | Every validator in the image without a terminology server |
| `scripted/docker-compose.yml` | The engine, the web app and its worker on one database, redis, the validator, FHIRPath, HAPI (profile `bulk`) and nginx |
| `smart_client_stu2_2_expected.json`, `bulk_data_v200_client_expected.json` | The suites' expected results |

The driver and the inputs are `scripts/inferno_session.py` and `scripts/inferno_scripted_inputs.py`.
