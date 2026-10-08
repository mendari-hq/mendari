# Mendari

**Open infrastructure for reusable healthcare decision logic: a "Hugging Face for healthcare
decisions."**

Mendari turns healthcare workflow decisions (route this message, is this prior-auth packet
complete, does this AI answer need a clinician) into standardized, testable, versioned,
governed software components called **Decision Packs**. A pack does not depend on any one
model. Rules, [Jev](#jev), classical ML and (soon) LLMs all answer the same questions through
the same interface, and every decision ends in an explicit `AUTO`, `HUMAN_REVIEW`, `ABSTAIN` or
`BLOCK` with a hash-chained audit record.

> **Status: research preview** (roadmap milestones v0.1–v0.5 done; package 0.1.0, not released).
> Everything here runs on synthetic data. It is not a medical device. Organization sign-in and PHI
> mode are new (v0.6) and have not had a security review, so it must not be used with real PHI or
> real patients yet.

> Mendari was called **OpenHealth Hub** until 2026-09-30. Settings under the old `OPENHEALTH_`
> prefix still work (a `MENDARI_` setting wins when both are set), and so do old pack ids
> (`openhealth/…`) in the providers and packs settings, so existing deployments need no change.

```text
Clinical/EHR data (FHIR R4, CDS Hooks, REST)
        │
        ▼
  input schema ──► provider slots ──► decision policy ──► AUTO | HUMAN_REVIEW | ABSTAIN | BLOCK
                   rules │ Jev │ ML      thresholds, rules,        │
                   (swappable variants)  risk-level caps           ▼
                                                  audit trail · review queue · FHIR Task
```

## What's in it

| Item | Where |
|---|---|
| Decision Pack specification | [`docs/spec/decision-pack-0.2.md`](https://github.com/Junebugg1214/mendari/blob/main/docs/spec/decision-pack-0.2.md) (0.1: [`decision-pack-0.1.md`](https://github.com/Junebugg1214/mendari/blob/main/docs/spec/decision-pack-0.1.md)), [`src/mendari/core/spec.py`](https://github.com/Junebugg1214/mendari/blob/main/src/mendari/core/spec.py) |
| Mendari runtime | [`src/mendari/core/decision.py`](https://github.com/Junebugg1214/mendari/blob/main/src/mendari/core/decision.py), [`policy.py`](https://github.com/Junebugg1214/mendari/blob/main/src/mendari/core/policy.py) |
| Providers: rules, naive Bayes, Jev, LLMs (Vercel AI Gateway, or OpenAI-compatible local models), criteria, CQL, dosing; combined with `any_of`, `cascade`, `vote` | [`src/mendari/providers/`](https://github.com/Junebugg1214/mendari/tree/main/src/mendari/providers) |
| FHIR adapter + CDS Hooks | [`src/mendari/standards/`](https://github.com/Junebugg1214/mendari/tree/main/src/mendari/standards) |
| Patient Inbox Router reference pack | [`packs/patient-inbox-router/`](https://github.com/Junebugg1214/mendari/tree/main/packs/patient-inbox-router) ([Decision Card](https://github.com/Junebugg1214/mendari/blob/main/packs/patient-inbox-router/DECISION_CARD.md)) |
| Clinical Trial Prescreener reference pack (v0.3) | [`packs/clinical-trial-prescreener/`](https://github.com/Junebugg1214/mendari/tree/main/packs/clinical-trial-prescreener) ([Decision Card](https://github.com/Junebugg1214/mendari/blob/main/packs/clinical-trial-prescreener/DECISION_CARD.md)) |
| Referral Router reference pack (v0.3) | [`packs/referral-router/`](https://github.com/Junebugg1214/mendari/tree/main/packs/referral-router) ([Decision Card](https://github.com/Junebugg1214/mendari/blob/main/packs/referral-router/DECISION_CARD.md)) |
| Prior Authorization Preflight reference pack (v0.3) | [`packs/prior-auth-preflight/`](https://github.com/Junebugg1214/mendari/tree/main/packs/prior-auth-preflight) ([Decision Card](https://github.com/Junebugg1214/mendari/blob/main/packs/prior-auth-preflight/DECISION_CARD.md)) |
| AI Guard reference pack (v0.3) | [`packs/ai-guard/`](https://github.com/Junebugg1214/mendari/tree/main/packs/ai-guard) ([Decision Card](https://github.com/Junebugg1214/mendari/blob/main/packs/ai-guard/DECISION_CARD.md)) |
| Human review / abstention | policy outcomes + [`core/audit.py`](https://github.com/Junebugg1214/mendari/blob/main/src/mendari/core/audit.py) review queue |
| Evaluation harness | [`src/mendari/evals/`](https://github.com/Junebugg1214/mendari/tree/main/src/mendari/evals) |
| Audit trail | SQLite or Postgres, append-only hash-chained events, `mendari audit verify` |
| Synthetic healthcare dataset | [`src/mendari/sandbox/synthetic_inbox.py`](https://github.com/Junebugg1214/mendari/blob/main/src/mendari/sandbox/synthetic_inbox.py), [`synthetic_trial.py`](https://github.com/Junebugg1214/mendari/blob/main/src/mendari/sandbox/synthetic_trial.py), [`synthetic_referral.py`](https://github.com/Junebugg1214/mendari/blob/main/src/mendari/sandbox/synthetic_referral.py), [`synthetic_prior_auth.py`](https://github.com/Junebugg1214/mendari/blob/main/src/mendari/sandbox/synthetic_prior_auth.py), [`synthetic_guard.py`](https://github.com/Junebugg1214/mendari/blob/main/src/mendari/sandbox/synthetic_guard.py) |
| Dashboard | `mendari serve` → http://127.0.0.1:8000: every pack on the search path. Overview, a review queue by priority, the audit trail with each record in a drawer, evaluations (provider leaderboard, benchmark report with charts, runs on your datasets), feedback with the right answer, the Decision Card, and Try it (a form for every pack, generated from its input schema for record packs, starting from the pack's behaviour tests, with the JSON one click away). In memberships mode, an organization's administrators also get Access (members, invitations, requests to join), and an invitation link opens its own page (`/invite`). Light and dark themes; times in the deployment's time zone (US Eastern by default). |

The five reference packs are examples built on synthetic data, and each says so on every surface
(its Decision Card, the dashboard, CDS Hooks cards, the registry):
**Synthetic example. Not clinically reviewed. Not for patient care.**

## Install

Mendari is not on PyPI yet: `pip install mendari` will work from the first release, 0.1.0.
Until then, install it from a clone of this repository. You need Python 3.11+, and for
development [uv](https://docs.astral.sh/uv/):

<!-- check: skip private-repo: CI's checkout is this clone, and the quickstart job runs uv sync in it -->
```bash
git clone https://github.com/Junebugg1214/mendari.git
cd mendari
uv sync                          # the package, the server, and the test and lint tools
```

Without uv, in the clone, into any Python 3.11+ virtual environment:

<!-- check: run install -->
```bash
pip install .                    # the library and the `mendari` command
pip install '.[server]'          # with the `server` extra: `mendari serve` and `registry serve`
```

The `server` extra adds uvicorn, which `mendari serve`, `mendari registry serve` and the
sandbox payer need; `uv sync` includes it. Vercel runs the ASGI app directly and needs no extra.
Without it, `mendari serve` says so in one line.

**Where packs come from.** Every command that takes a pack (`list`, `test`, `serve`,
`Decision.from_hub`, ...) looks in this order:

1. the folders in `MENDARI_PACKS_PATH`, separated by `:` (`;` on Windows), each searched for
   `pack.yaml` files up to four levels deep;
2. packs pulled from a registry (`mendari pull`, verified, cached in `~/.cache/mendari/packs`);
3. the reference packs that come with Mendari (`packs/` in a clone);
4. with `MENDARI_COMMUNITY_PACKS=on` (default off), the community packs in a clone's
   `community-packs/`, each at `community-packs/<name>/pack.yaml` with id `community/<name>`.
   A community pack recommends and never automates, is at most risk level 1, keeps only a hash
   of its inputs, and decides with local providers unless a provider setting names it
   (`community/<name>:slot=variant`); its pinned model and `max_tokens` give way to the
   deployment's. Wherever it shows (`/api/packs`, the dashboard, Try it, the review queue, the
   SMART patient view, its Decision Card, CDS Hooks discovery and cards, its FHIR CodeSystems,
   the `/docs` landing) it is labelled "Community · not clinically reviewed"; the reference
   packs say "Maintained by Mendari". How to contribute
   one is in the guide
   [Contributing a community pack](https://github.com/Junebugg1214/mendari/blob/main/docs/guides/contributing-a-pack.md),
   and what CI checks (`mendari community-check`) in
   [community-packs/README.md](https://github.com/Junebugg1214/mendari/blob/main/community-packs/README.md).

When several packs on the path have one id, the newest version is served (a pack pulled from a
registry can be newer than the one that came with Mendari). A community pack is the exception:
its id is `community/<name>`, never one of the reference packs' (`mendari/`, `openhealth/`),
and a second pack claiming its id is not served, whatever its version; both are listed under
`invalid` in `/api/health`. A pack anywhere whose id is in `community/` gets the community
limits.

A pack directory works anywhere too: `mendari test ./my-pack`. To serve your own packs, point
the path at them: `MENDARI_PACKS_PATH=./my-packs uv run mendari serve`. On a server,
`MENDARI_PACKS` (comma-separated ids) narrows what is served to the packs it names.

## Quickstart

In the clone, with uv ([Install](#install)):

<!-- check: run quickstart -->
```bash
uv sync
uv run mendari demo
```

The demo routes synthetic patient messages, simulates reviewers working the queue, verifies
the audit chain, and then runs **local validation** of the public pack against a simulated
deploying site. It needs no keys, so the keyword rules alone screen for emergencies, and the
inbox's policy then sends every message to a person; with a model in the screen
([Jev](#jev)), routine messages are automated:

```text
Public benchmark (test, n=600):                 99.3%
Local performance (local-site-example, n=400):  93.2%

Performance degradation detected (-6.1 pts overall; tolerance 3.0):
  - topic=bill: 100.0% -> 73.7% (n=38)
  - label billing: 100.0% -> 80.8% (n=52)
  - topic=refill: 100.0% -> 86.5% (n=52)
  - language=es: 92.3% -> 79.8% (n=119)
  ...
Emergency screen at the local site: 18/18 detected, 0 automated.
```

The site writes Spanish billing and refill requests in ways the public data never saw. The
comparison finds where the pack degrades, and the policy sent every misrouted message to a
human instead of automating it. (The synthetic public benchmark is template-generated and
overstates real-world accuracy. It is a regression test, not a performance estimate.)

### Write your own pack

No programming needed: a pack is YAML. The [authoring guide](https://github.com/Junebugg1214/mendari/blob/main/docs/guides/authoring-a-pack.md)
walks a clinician through it:

<!-- check: run quickstart -->
```bash
uv run mendari new refill-router --id my-clinic/refill-router
uv run mendari test refill-router
uv run mendari eval refill-router --dataset examples
```

### Python

<!-- check: run python inbox -->
```python
from mendari import Decision

router = Decision.from_hub("mendari/patient-inbox-router")
result = router({"message": {"body": "I've had chest pain for 30 minutes spreading to my arm"}})

result.outcome          # Outcome.HUMAN_REVIEW
result.recommendation   # 'nurse'
result.priority         # Priority.STAT
result.reasons          # ('rule possible-emergency: Possible emergency symptoms. ...',)
result.answers["urgency"].evidence   # ('urgent:chest-pain',)
```

Swap a provider without touching the pack's contract, policy or tests, e.g. to Mendari + Jev,
which routine messages are automated with:

<!-- check: run python inbox -->
```python
Decision.from_hub(
    "mendari/patient-inbox-router", providers={"safety_screen": "rules_and_jev", "router": "cascade"}
)
```

### CLI

<!-- check: run cli -->
```bash
uv run mendari list                                      # packs on the search path
uv run mendari validate mendari/patient-inbox-router
uv run mendari test mendari/patient-inbox-router         # the pack's own behaviour tests
uv run mendari decide mendari/patient-inbox-router --text "Can I pay my bill online?"
uv run mendari eval mendari/patient-inbox-router --dataset datasets/local-site-example.jsonl
uv run mendari card mendari/patient-inbox-router         # render the Decision Card
uv run mendari test mendari/clinical-trial-prescreener
uv run mendari eval mendari/clinical-trial-prescreener --dataset exclusions_holdout
uv run mendari --version
```

Your own site's data, and the server:

<!-- check: skip placeholder: my-site.jsonl is your own data, and serve runs until stopped; check_doc_snippets.py starts the same app in CI -->
```bash
uv run mendari eval mendari/patient-inbox-router --dataset my-site.jsonl --fail-on-degradation
uv run mendari serve                                     # API + CDS Hooks + dashboard
                                                         # (developer docs: /docs)
```

Then, in the folder the server ran in, verify the audit chain of the database it wrote:

<!-- check: run server-db -->
```bash
mendari audit verify --db var/mendari.db
```

**Errors and exit codes.** A command that cannot do what was asked prints one line per problem
(`error: ...`, naming the file, the field or the flag, and what to do) and exits 1, never a
traceback; `--debug` on any command (or `MENDARI_DEBUG=1`; `0` or `off` keeps it off) shows the
full traceback instead. Exit 1 is also a failed check (a failing behaviour test, a broken chain,
`monitor`'s alerts); `eval --fail-on-degradation` exits 2 on a regression; Ctrl-C exits 130 (a
parallel `eval` stops sending the rest of the dataset); a closed pipe (`| head`) exits 141, with
no error line.

**The loop cannot pass by accident.** `mendari test` refuses a behaviour test whose `expect` is
missing, empty, misspelt or names what the pack can never decide, and fails when the pack has
no tests; `mendari validate` also reads every dataset the pack declares. A `--provider` (or
`leaderboard --run`) setting that applies to nothing in the pack is an error; from
`MENDARI_PROVIDERS`, which a deployment shares across packs, it is a warning (and, on a server,
reported in `/api/health` as `provider_settings_unused`).

**Read-only commands.** `audit verify`, `audit export`, `feedback export`, `monitor` and
`access check` (with `access org list`, `members` and `requests`) open the server's database
read-only: they never create, migrate or write it, and a `--db` that does not
exist is an error, never a new empty database that "verifies".

### REST API

With `uv run mendari serve` running, in another terminal (the examples need `curl` and `jq`):

<!-- check: skip settings: sets the two variables check_doc_snippets.py sets for each pass -->
```bash
export MENDARI=http://localhost:8000   # the server
export MENDARI_KEY=                    # with sign-in on: an API key (ohk_…); open mode ignores it
```

List the packs, decide on a synthetic patient message (one of the pack's behaviour tests), and
read the decision's audit record:

<!-- check: snippet quickstart.sh/packs -->
```bash
curl -sS --fail-with-body "$MENDARI/api/packs" -H "Authorization: Bearer $MENDARI_KEY" \
  | jq '.[] | {id, version, risk}'
```

<!-- check: snippet quickstart.sh/decide -->
```bash
decision=$(curl -sS --fail-with-body -X POST \
  "$MENDARI/api/packs/mendari/patient-inbox-router/decide" \
  -H "Authorization: Bearer $MENDARI_KEY" -H 'content-type: application/json' \
  -d '{"message": {"subject": "Refill request", "body": "Hi, I need a refill on my lisinopril. I have about three pills left. Thanks"}}')
jq '{id, outcome, recommendation, priority, reasons}' <<<"$decision"
id=$(jq -r .id <<<"$decision")
```

<!-- check: snippet quickstart.sh/record -->
```bash
curl -sS --fail-with-body "$MENDARI/api/audit/$id" -H "Authorization: Bearer $MENDARI_KEY" \
  | jq '{outcome, review_status, events: [.events[].event]}'
```

With no model in the inbox's urgency screen (the default: keyword rules only), the refill is
routed to the medical assistant, but a person confirms it: the decision prints
`"outcome": "HUMAN_REVIEW"` with the reason `automation requires safety_screen = rules_and_jev or
rules_and_llm (running: rules)`. The inbox automates only while a model screens for emergencies
too ([Jev](#jev)); then the same message is `AUTO`. Check `outcome`, not only the status code: an
input that fails the pack's schema is a recorded decision with `outcome: BLOCK`. Whether sign-in is on, and what your key may do:

<!-- check: snippet auth.sh/mode -->
```bash
curl -sS --fail-with-body "$MENDARI/api/auth/me" | jq '{mode, signed_in}'
```

<!-- check: snippet auth.sh/me -->
```bash
curl -sS --fail-with-body "$MENDARI/api/auth/me" -H "Authorization: Bearer $MENDARI_KEY" \
  | jq .principal
```

On a running server, `/docs` is a developer landing with these examples (the server's address
filled in, a copy button on each), FHIR in and out, and what every call needs; it is public, also
with sign-in on. The full reference is `/docs/reference`, the [API guide](https://github.com/Junebugg1214/mendari/blob/main/docs/guides/api.md) has
the same examples as whole scripts, and FHIR is under
[Healthcare standards](#healthcare-standards). Every `curl` example in this README (but `vercel
curl`, which goes through a deployment) is a step of the scripts in
[`src/mendari/dashboard/snippets/`](https://github.com/Junebugg1214/mendari/tree/main/src/mendari/dashboard/snippets) (a test keeps them equal),
and CI runs those scripts against a local server, in open mode and with a `service` key
(`scripts/check_doc_snippets.py`), so an example that stops working fails the build.

### Guarding an LLM's replies

A pack with a `guard` section (the AI Guard reference pack is one) checks a model's draft
before anyone reads it. The draft is `released` only when the decision is automated with a
release recommendation; otherwise it is `held_for_review` or `blocked`, with the reasons (and, when
blocked, the pack's `fallback`). Here, a dose above the daily maximum is blocked:

<!-- check: snippet guard.py/check -->
```python
from mendari import Decision
from mendari.guard import check

guard = Decision.from_hub("mendari/ai-guard")
question = "My back hurts. What can I take?"
draft = "Take 2 Advil tablets of 400 mg three times a day."  # what your model wrote
result = check(guard, {"question": question, "draft": draft})
print(result.status)  # blocked: never send a blocked draft
print(result.decision.fallback)  # what to do instead
```

Send `result.reply` only when `result.status` is `released`; queue a `held_for_review` draft for a
clinician with `result.decision.reasons`. Over HTTP, with `mendari serve` running:

<!-- check: snippet guard.sh/check -->
```bash
curl -sS --fail-with-body -X POST "$MENDARI/api/packs/mendari/ai-guard/guard" \
  -H "Authorization: Bearer $MENDARI_KEY" -H 'content-type: application/json' \
  -d '{"question": "My back hurts. What can I take?", "draft": "Take 2 Advil tablets of 400 mg three times a day."}' \
  | jq '{status, reply, outcome, recommendation, fallback}'
```

### Container images and on-prem templates

`docker build .` builds the engine with every pack, installing exactly `uv.lock` on a base image
pinned by digest. `--build-arg PACK=mendari/referral-router` builds one that serves only that pack
(`MENDARI_PACKS`). A release publishes both kinds to GitHub Container Registry, for linux/amd64
and linux/arm64 (workflow `release`, which only a maintainer starts, never a tag push), exactly
as tested; nothing is published before that. [`deploy/`](https://github.com/Junebugg1214/mendari/tree/main/deploy) has
templates for a site's own infrastructure: docker compose on one server and Kubernetes. Both come
with Postgres, your identity provider, PHI mode and the daily retention and monitoring jobs.

Mendari's own code is Apache-2.0; an image also holds third-party software under other licences
(Debian's packages, and the libraries the PostgreSQL driver bundles, some under the GPL or LGPL).
Each image carries them in `/licenses`: Mendari's `LICENSE` and `NOTICE`, `THIRD_PARTY_NOTICES.md`
(every component, its version, its licence, where its notices are and where its source is),
`SOURCE.md` and the texts of what bundles no notice of its own; Debian's copyright files stay in
`/usr/share/doc`. A release publishes each image's corresponding source beside it, as
`<image>:<tag>-sources`, with the image's SBOM (SPDX, every package with its source location) in
its `/sources/sbom/`; the SBOM BuildKit attaches in the image's index lists the same packages and
licences, without their source locations. `scripts/check_image_licences.py` checks all of it
(CI's `docker` and `image-sbom` jobs), and the release pushes no image that fails it or whose
SBOM names a licence that is unknown or not allowed. The image builds with BuildKit or Docker's
classic builder alike.

### Docker sandbox

<!-- check: skip docker: needs the Docker sandbox; CI job sandbox-e2e runs it -->
```bash
docker compose up --build
```

This starts a HAPI FHIR R4 server seeded with synthetic cases as US Core 6.1.0 resources:
inbox Patients and Communications, and referral Patients, ServiceRequests, notes, reports and
labs. Referral dates are moved to today, so the windows the pack checks stay the same. Then it
starts the Mendari engine (REST, FHIR, CDS Hooks, Jev adapter, review queue, audit, dashboard at
http://localhost:8000), a SMART launcher (smart-launcher-v2, in front of HAPI) and a workflow
worker. The worker routes each in-progress Communication and writes a FHIR `Task` back to the
server. No real PHI is involved.

A SMART EHR launch, scripted end to end (EHR and browser in one), decides a patient's referrals
or messages:

<!-- check: skip docker: needs the Docker sandbox; CI job sandbox-e2e runs it -->
```bash
docker compose exec engine mendari sandbox smart-launch --pack mendari/referral-router \
  --engine http://engine:8000 --launcher http://launcher --fhir http://fhir:8080/fhir
```

The profile `signin` adds a second engine with sign-in on (port 8001) where the launcher is
registered to decide for organization `pilot`; CI's `sandbox-e2e` checks that its launches
decide there, chained as the launcher's `<issuer>|<subject>`:

<!-- check: skip docker: needs the Docker sandbox; CI job sandbox-e2e runs it -->
```bash
docker compose --profile signin up -d --build --wait engine launcher engine-signin
docker compose exec engine-signin mendari smart check http://launcher/v/r4/fhir
docker compose exec engine-signin mendari sandbox smart-launch --pack mendari/referral-router \
  --engine http://engine-signin:8000 --launcher http://launcher --fhir http://fhir:8080/fhir \
  --expect-org pilot --db /data/mendari.db
```

**Bulk FHIR into local validation.** Export a site's data, then evaluate a pack on it with the
site's own labels (ground truth from chart review, joined by an identifier system):

<!-- check: skip ehr: needs an EHR's Bulk FHIR endpoint and a registered key; CI job sandbox-e2e runs bulk-check -->
```bash
mendari bulk export --fhir https://ehr.example.org/fhir --group my-cohort --out export/ \
  --client-id my-app --private-key key.pem --key-id k1        # SMART Backend Services
mendari bulk dataset --pack mendari/referral-router --ndjson export/ \
  --labels chart-review.jsonl --key-system https://site.example/mrn --out local.jsonl
mendari eval mendari/referral-router --dataset local.jsonl
```

The CI job `sandbox-e2e` runs the scripted launch for the inbox and referral packs on every
change. It also runs `mendari sandbox bulk-check`: an export of the sandbox must evaluate
exactly like the same cases read from the dataset file.

## Healthcare standards

FHIR inputs (`/api/packs/{pack}/fhir/decide`, CDS Hooks, `mendari decide --fhir`) first pass a
**structural R4 check** against HL7's R4 JSON Schema (digest-pinned and patched by
`scripts/fhir_r4_schema.py`): an unknown element or resource type, an element of the wrong JSON
type, or a primitive without its format is a `400` in every mode. This is a structural check,
not validation. Then they are checked against **US Core 6.1.0** in process. The checks cover
required elements, required bindings, the required category slices and the profiles'
`us-core-*` invariants. They are generated from the official packages by
`scripts/us_core_subset.py`. A complex element R4 itself requires, or a code outside an R4
required value set, is an issue of the same kind, labelled with the base R4 profile.
`MENDARI_FHIR_VALIDATION` chooses what happens to these issues:

| Setting | Effect |
|---|---|
| `warn` (default) | The decision is unchanged. The issues come back in `decision.input_issues` and are audited, as paths without values. |
| `enforce` | An input with an error is blocked before any provider sees it, like a JSON Schema failure. |
| `off` | No profile or required-element checks (the structural check still runs). |

**The FHIR base, `/fhir`.** Mendari is a decision engine, not a FHIR data server: it stores and
serves no clinical resources. `GET /fhir/metadata` is its CapabilityStatement, built from the
routes that are mounted. `POST /fhir/$decide` (defined at `/fhir/OperationDefinition/decide`)
takes `Parameters` with the `pack` and the `data` Bundle, and returns the decision as a FHIR
`collection` Bundle: a **GuidanceResponse** (act on `outputParameters.outcome`, not on
`status`), a RequestGroup and the work-queue **Task** (under BLOCK or ABSTAIN, the pack's
fallback), the answers as Parameters, findings as an OperationOutcome, and the audit trail's
Provenance. References to the input are the absolute URLs its entries' `fullUrl`s give, or else
`Type/id`, relative to your own FHIR server. The codes it uses are Mendari CodeSystems and
NamingSystems, generated from the code and the served packs (`standards/terminology.py`) and
served at `/fhir/CodeSystem/{id}` and `/fhir/NamingSystem/{id}`. FHIR routes speak FHIR R4 JSON
only (`415`/`406` otherwise).

<!-- check: snippet fhir.sh/metadata -->
```bash
curl -sS --fail-with-body "$MENDARI/fhir/metadata" | jq '{fhirVersion, software}'
```

With a synthetic patient message as a Bundle (with sign-in on, `MENDARI_KEY` as in the
[REST quickstart](#rest-api)):

<!-- check: snippet fhir.sh/decide -->
```bash
bundle=$(curl -sS --fail-with-body -X POST "$MENDARI/fhir/\$decide" \
  -H "Authorization: Bearer $MENDARI_KEY" -H 'content-type: application/fhir+json' \
  -d '{"resourceType": "Parameters", "parameter": [
    {"name": "pack", "valueString": "mendari/patient-inbox-router"},
    {"name": "data", "resource": {"resourceType": "Bundle", "type": "collection", "entry": [
      {"fullUrl": "urn:uuid:7f1c2a4e-0d1b-4c55-9a51-3c1e2d000001", "resource": {
        "resourceType": "Patient", "id": "example-patient",
        "meta": {"tag": [{"system": "https://mendari.co/fhir/sandbox", "code": "synthetic"}]},
        "identifier": [{"system": "https://mendari.co/fhir/synthetic-case", "value": "1"}],
        "name": [{"family": "Synthetic", "given": ["Example"]}],
        "gender": "unknown", "birthDate": "1984-06-15"}},
      {"fullUrl": "urn:uuid:7f1c2a4e-0d1b-4c55-9a51-3c1e2d000002", "resource": {
        "resourceType": "Communication", "id": "example-message", "status": "in-progress",
        "subject": {"reference": "Patient/example-patient"}, "sent": "2026-09-29T13:00:00Z",
        "payload": [{"contentString": "Can I get a refill of my blood pressure medicine?"}]}}]}}]}')
jq '[.entry[].resource.resourceType]' <<<"$bundle"  # a GuidanceResponse first
jq '.entry[].resource | select(.resourceType == "Parameters").parameter[]
  | select(.name == "outcome")' <<<"$bundle"       # act on the outcome, not on `status`
id=$(jq -r '.entry[0].resource.identifier[0].value' <<<"$bundle")  # the decision id
```

**Decisions as FHIR, read and searched** (read-only, your organization's only; see
[ADR-0003](https://github.com/Junebugg1214/mendari/blob/main/docs/decisions/ADR-0003-a-served-task-is-a-decision.md)). Each decision is a
**Task**: `requested` while it waits for a reviewer in Mendari's queue, `completed` once the
answer is final, `failed` when the input was refused. Its versions follow the audit chain
(created, reviewed, outcome recorded), so `GET /fhir/Task/{id}/_history/{n}` reads one. It
carries no input text and no patient or subject references, and searching by `patient`,
`subject` or `focus` is refused (`400`). The audit trail is searchable as **AuditEvent** and
**Provenance**, in chain order. Search takes GET or a form POST to `/_search`, pages with a
`next` link (50 by default, at most 500), reads date-only values as the UTC day, and ignores
unknown parameters with a warning entry (`Prefer: handling=strict` makes them a `400`).

<!-- check: snippet fhir.sh/search -->
```bash
curl -sS --fail-with-body "$MENDARI/fhir/Task?status=requested&_total=accurate" \
  -H "Authorization: Bearer $MENDARI_KEY" | jq .total                   # the review queue
curl -sS --fail-with-body "$MENDARI/fhir/Task/$id/_history/1" \
  -H "Authorization: Bearer $MENDARI_KEY" | jq '{id, status, meta}'     # as first decided
curl -sS --fail-with-body "$MENDARI/fhir/AuditEvent?entity=Task/$id" \
  -H "Authorization: Bearer $MENDARI_KEY" | jq '[.entry[].resource.id]' # its audit trail
curl -sS --fail-with-body "$MENDARI/fhir/Provenance?target=Task/$id" \
  -H "Authorization: Bearer $MENDARI_KEY" | jq '[.entry[].resource.id]'
```

The audit trail exports as FHIR R4. Each event in the hash chain becomes an **AuditEvent**, and
each decision, review and downstream outcome becomes a **Provenance** on the decision's `Task`.
They carry ids, codes and the chain's hashes, never case text:

<!-- check: snippet fhir.sh/trail -->
```bash
curl -sS --fail-with-body "$MENDARI/api/audit/$id/fhir" -H "Authorization: Bearer $MENDARI_KEY" \
  | jq '[.entry[].resource.resourceType]'
```

The whole trail, for a signed-in administrator (or in open mode; never with a key), and from
the database with the CLI:

<!-- check: snippet fhir.sh/export -->
```bash
curl -sS --fail-with-body \
  "$MENDARI/api/audit/export/fhir?pack_id=mendari/patient-inbox-router&since=2026-09-01" \
  | jq -c '{resourceType, id}'
```

<!-- check: run server-db -->
```bash
mendari audit export --db var/mendari.db --out audit.ndjson             # FHIR NDJSON
```

CDS Hooks services fetch the prefetch data an EHR leaves out, using the pack's own prefetch
templates, the request's `fhirServer` and its `fhirAuthorization` token. They do this only when
that server's origin is listed in `MENDARI_FHIR_ALLOWED_SERVERS` (for example
`https://ehr.example.org,http://fhir:8080`). The list is empty by default, and every other server
is refused.

**SMART on FHIR EHR launch.** Register `https://<mendari>/smart/launch?pack=<pack id>` as
the app's launch URL and `https://<mendari>/smart/callback` as its redirect URI in the EHR.
Then set `MENDARI_PUBLIC_URL`, put the EHR's FHIR server and authorization server on
`MENDARI_FHIR_ALLOWED_SERVERS`, and:

| Setting | Effect |
|---|---|
| `MENDARI_SMART_CLIENT_ID` | The app's client id at the EHR; the launch is off until it is set |
| `MENDARI_SMART_CLIENT_SECRET` | For a confidential client |
| `MENDARI_SMART_SCOPES` | Default `launch openid fhirUser patient/*.rs`; with sign-in on it must ask for `openid fhirUser`, and `offline_access` is always refused (at the launch, never at startup) |
| `MENDARI_SMART_EHRS` | The EHRs that may launch, `<FHIR base>=<organization>:view` or `:decide`, comma-separated (the base as the EHR sends it in `iss`; two tenants on one host are two entries). The organization must be one your people sign in to: the value of their `MENDARI_OIDC_ORG_CLAIM` claim, else `default` (one organization: `https://ehr.example.org/fhir=default:decide`); in memberships mode, an organization with a record; without the claim and with sign-in on, `mendari smart check <base>` fails any other organization (in memberships mode it warns, and fails only an id no record can have). With sign-in on, a launch is a principal of its EHR's organization that may view the patient, or view and decide; a launch from any other EHR, or by a user who is not a Practitioner or PractitionerRole, is refused. Unset with sign-in on: every launch is refused. A malformed value, or a base off the allow-list, fails at startup |

With sign-in on, the EHR's verified sign-in names the clinician (the chain's name: their
verified email, else `<issuer>|<subject>`), and the launch's cookie reaches only the launch's
own routes (an `Authorization` header decides alone there too): no review queue, audit trail
or packs. `MENDARI_SMART_EHRS` is read at startup, so unregistering an EHR ends its live
launches at the next restart or redeploy; signing out of Mendari ends the browser's launch, a
later launch in the same browser ends the earlier one, and a launch made while sign-in was off
starts again once it is on. In open mode with no EHR registered, any allow-listed EHR
launches, as before. With sign-in on, a browser that only launched opens the dashboard on the
patient view alone, with the decision shown in place (the organization's reviewers read it in
the audit trail).

The launch is OAuth 2 with PKCE (S256). The `id_token` is verified against the server's JWKS
(`iss`, `aud`, `exp`, `nonce`). The access token stays on the server, sealed in the session
row under a key derived from the browser's opaque, HttpOnly, SameSite=Lax `__Host-mendari_smart`
session cookie, so the database alone never reveals it. The session ends like a person's (and
when the EHR's token does, or the EHR answers `401` to it), and the callback works only in the
browser that started the launch. No refresh token is kept: `offline_access` in
`MENDARI_SMART_SCOPES` is refused at launch. The dashboard opens on the launch's patient,
pinned to that launch (`#patient/<launch_id>`; a window whose launch a later one in the same
browser replaced says so, and shows no patient: it sends `X-Mendari-Launch` with the requests
below, and the server answers `409` to a replaced launch), and says "This EHR launch ended.
Open Mendari again from the EHR." when the launch's routes answer `401`:
- `GET /api/smart/patient`;
- their messages or referrals: `GET /api/smart/focus?pack_id=`;
- deciding one: `POST /api/smart/decide`, which reads the record through the pack's CDS prefetch
  templates and audits the decision.

SMART is off until the client id is set. Launch it in a new window or the EHR's embedded
window: inside an iframe the launch fails safely. Before go-live, `mendari smart check <FHIR
base>` reports, with the deployment's settings, the base's registration key, whether it is
allowed and registered, the scopes, and the EHR's discovery, PKCE S256 and keys.

**CQL.** A pack can compute answers with a CQL library (provider type `cql`), evaluated on a
CQL engine at `MENDARI_CQL_URL` (HAPI FHIR with Clinical Reasoning; the sandbox's HAPI has
it on). The trial pack's `cql` variant holds its criteria in CQL. `mendari agreement` shows
that it answers like `criteria` on every case, and the CI job `cql` checks it.

**Da Vinci CRD.** The Prior Authorization Preflight's `order-sign` service also returns, for
each order it decides, a system action that updates the order with CRD's `coverage-information`:
covered, whether prior authorization and documentation are needed, the patient's Coverage and
an assertion id, which is the decision id. The HL7 validator checks it against CRD 2.2.1 in CI.
A pack declares this with `cds_hooks.coverage_information`.

**Da Vinci DTR.** When documentation is missing, that coverage information links a questionnaire.
`POST /fhir/Questionnaire/$questionnaire-package` (with the Coverage and the order) returns it: the
documentation the payer asks for, for that service and payer, pre-answered from the pack's
decision, which is found by the CRD assertion id on the order. A clinician completes only what is
missing. It is validated against DTR 2.2.0 in CI, and a pack declares it with
`cds_hooks.questionnaire`.

**Da Vinci PAS.** For a complete request (`submit_ready`), `POST /api/packs/{pack}/pas` (or
`mendari pas build`) builds the PAS request Bundle. It holds a `preauthorization` Claim, the
patient, Coverage, payer, requesting organization and order from the EHR, and the X12 service
type and place of service. Nothing is filled in: a missing element is an error that names it.
With `submit`, a decision that was automated, or that a reviewer resolved as `submit_ready`,
goes to the payer's `Claim/$submit` (`MENDARI_PAS_PAYER_URL`, allow-listed),
and the payer's decision is chained on the decision as codes: its outcome and review actions
(text in their place is dropped). Its authorization number and disposition are kept as a note. The sandbox has a mock payer (`mendari sandbox payer`, compose service `payer`).
The request and the payer's answer validate against PAS 2.2.1 in CI.

**OMOP CDM.** Research data in OMOP (SQLite or Postgres, with an optional schema such as `cdm`)
becomes record-pack inputs, one per person, for population-level evaluation:

<!-- check: skip database: needs an OMOP CDM database; CI job reference-pack runs omop-check -->
```bash
mendari omop run --db "$OMOP_URL" --schema cdm --pack mendari/clinical-trial-prescreener
mendari omop dataset --db "$OMOP_URL" --schema cdm --pack mendari/clinical-trial-prescreener \
  --labels chart-review.jsonl --out local.jsonl    # then: mendari eval ... --dataset local.jsonl
```

`omop run` reports the outcome mix and each question's answers, with cells under 10 people
suppressed. `mendari sandbox omop-check` writes a dataset into OMOP, reads it back and checks
that it evaluates identically (in CI).

The CI job `fhir-conformance` runs the HL7 FHIR validator over the same fixtures and a sample of
the audit export. Everything
Mendari treats as conforming must pass it, and everything the in-process checks flag must
fail it.

## Documentation

Every page in [`docs/`](https://github.com/Junebugg1214/mendari/tree/main/docs) builds into a documentation site with MkDocs Material:

<!-- check: skip long-running: mkdocs serve runs until stopped; CI job docs builds the same site strictly -->
```bash
uv sync --group docs
uv run mkdocs serve    # http://127.0.0.1:8000
```

CI builds it in strict mode, so a broken link fails. It will be hosted once the repository is
public. Until then a running server documents itself: `/docs` is the developer landing (a
quickstart in three calls, FHIR, authentication), and `/docs/reference` the full API reference.
The [API guide](https://github.com/Junebugg1214/mendari/blob/main/docs/guides/api.md) covers the same ground in the site.
To contribute a pack, read
[Contributing a community pack](https://github.com/Junebugg1214/mendari/blob/main/docs/guides/contributing-a-pack.md);
why community packs work as they do is
[ADR-0007](https://github.com/Junebugg1214/mendari/blob/main/docs/decisions/ADR-0007-community-packs-by-pull-request.md).

## Security

[`SECURITY.md`](https://github.com/Junebugg1214/mendari/blob/main/SECURITY.md) says how to report a vulnerability and how to deploy safely. The
threat model ([`docs/security/threat-model.md`](https://github.com/Junebugg1214/mendari/blob/main/docs/security/threat-model.md)) lists every
threat with its mitigation and the test that checks it. On every change, the CI job `security`
audits the Python and Hub-site dependencies and runs an OWASP ZAP scan of the API with sign-in
on; GitHub Actions are pinned by commit and images by digest. Every response carries a strict
CSP and the usual isolation headers (HSTS off localhost), the developer landing and the API
reference included: the reference is Swagger UI served from vendored, pinned files, never a CDN,
and the landing loads nothing but its own files. Sessions end after 30 minutes idle
and 8 hours at most, behind `__Host-` cookies. A browser request that another site starts
reaches only public routes and pages, to read. A request
body is 4 MiB at most (`MENDARI_MAX_BODY_BYTES`; a `413` otherwise), and public routes take no
lock and do little work for a caller without a credential. With sign-in on, decisions, the AI
guard, evaluations and exports are rate limited per caller, and sign-in and the token endpoint per
address, in the database so every instance shares the count (`MENDARI_RATE_LIMITS`; a `429` with
`Retry-After`); organizations can have daily quotas (`MENDARI_ORG_DAILY_LIMITS`), over which
decisions fall back to human review, and each principal (a person, a key, a client, an EHR
launch) a daily cap on the decisions it asks for (`MENDARI_PRINCIPAL_DAILY_LIMIT`), past which its
requests are refused until midnight (`429`), so one leaked key cannot spend its organization's day. A security log goes to stdout as JSON lines: one access line
per API request and the security events (sign-ins and EHR launches, refusals, requests from other sites, rate
limits, key, client and governance changes, exports, evaluations, input and note reads), from an
allow-list of fields that has no room for a credential, a query value, a body or case text.
Every response carries an `X-Request-Id`, which JSON errors on `/api` repeat as `request_id`.
Under PHI mode the input hash is keyed and notes stay out of the audit chain (see PHI mode
below). Every security setting, with its default, is in
[deploying with real patient data](https://github.com/Junebugg1214/mendari/blob/main/docs/deploying-with-phi.md#8-settings-at-a-glance).

## The Hub

Packs travel as **signed archives**. A registry keeps them with **immutable versions**, and anyone
can pull and verify them:

Anyone with Mendari installed ([Install](#install); `pip install mendari` once 0.1.0 is on
PyPI) can pull from a registry they name with `mendari login --registry URL` or
`MENDARI_REGISTRY_URL`. There is no public registry yet; the docker sandbox runs one.

<!-- check: skip registry: needs a registry and a publisher token; CI job hub-e2e publishes the reference packs to a local registry through the same client and runs mendari pull and test -->
```bash
mendari keys generate --out keys/                           # a publisher key pair (Ed25519)
mendari login --registry https://hub.example.org --token <publisher token>
mendari keys register --namespace my-site --public-key keys/publisher.pub
mendari push packs/my-pack --key keys/publisher.key          # build, sign, publish

mendari pull mendari/referral-router                         # anyone, from that registry
mendari search referral --standard "CDS Hooks"
mendari eval mendari/referral-router --dataset local.jsonl --share "My site"
```

- **An archive** (`.ohpack`) is deterministic and carries a manifest of file hashes signed with
  the publisher's key.
- **`pull`** checks the archive digest the registry recorded, every file hash, the signature and
  the pack digest before caching the pack. Namespace keys are pinned on first use, and a changed
  key needs `--accept-new-key`. `Decision.from_hub` pulls on its own when a pack isn't local.
  Nothing an archive or a registry says chooses where a file is written: archive names are plain
  paths inside the pack, an archive is read with size caps checked as it expands, and the cache
  path is built only from a valid id and version that the signed manifest names.
- **Trust.** Installing a pack means trusting its author, as with any plugin: its patterns run
  on Python `re` with no time limit, so a pack can slow or stall a worker (a community pack's
  run on RE2, which never backtracks). What a pack cannot do
  (choose secrets or endpoints, reach outside its folder, stop the server, reach another pack's
  CQL) is in [SECURITY.md](https://github.com/Junebugg1214/mendari/blob/main/SECURITY.md#pack-supply-chain). Packs from others will run isolated
  once uploads or a public registry exist.
- **The registry** (`mendari registry serve`) has namespaces with a publisher type
  (community, verified, institutional) that its admins set after verification. `--share` sends
  a site's metrics, never its cases or their ids, in a closed format the registry checks
  (`mendari.site-report/1`); the registry lists them as unverified until sites sign them.
- **The website** (`hub/`, Next.js) searches packs and shows their cards and leaderboards. It
  does not show site reports until sites sign what they share.

The docker sandbox runs a registry on port 8100 (`scripts/hub_seed.py` publishes the five packs).

## Sign-in for organizations

By default the server is open: for local use and the sandbox, on `127.0.0.1`. Setting an
identity provider switches on sign-in, as the project's production deployment does (Google):

<!-- check: skip settings: settings for your identity provider, not commands; CI job pilot-check sets the same names -->
```bash
MENDARI_OIDC_ISSUER=https://login.example.org/realms/clinic   # your IdP (OpenID Connect)
MENDARI_OIDC_CLIENT_ID=mendari
MENDARI_OIDC_ROLE_MAP="clinicians=reviewer,informatics=admin" # IdP role or group -> role
MENDARI_PUBLIC_URL=https://mendari.example.org
```

With a provider that has no roles, such as Google, set `MENDARI_OIDC_ROLE_CLAIM=email` and map
verified emails to roles ([the Google setup](https://github.com/Junebugg1214/mendari/blob/main/docs/deploying-with-phi.md#2-sign-in-and-roles)).

People sign in with their organization's account, using PKCE and an `id_token` verified with the
IdP's keys, in the browser that started the sign-in only (a callback link sent to someone else
signs nobody in). A sign-in started on another name than `MENDARI_PUBLIC_URL`'s (an alias) is
sent there first, since the callback comes back to it. The session is a server-side row behind a `__Host-mendari_session` cookie
(plain `mendari_session` on localhost and dotless hosts). Each request works out the person's
roles again from the claims kept at sign-in and the settings as they are then, so a role taken
out of the map ends that access at once:

| Setting | Effect |
|---|---|
| `MENDARI_SESSION_IDLE_MINUTES` | A session ends after this long without a request (default 30; 5 to 120) |
| `MENDARI_SESSION_MAX_HOURS` | ...and this long after signing in, however busy (default 8; 1 to 12) |

The dashboard then says "Your session ended. Sign in again." and comes back to the same view.
Machines use **API keys** (`Authorization: Bearer ohk_…`), which signed-in admins
create in `POST /api/auth/keys` (never a key: managing credentials is people-only). A key is
shown once, stored hashed, revocable, and rotated with an overlap
(`POST /api/auth/keys/{id}/rotate`). It has the `viewer` or `service` role, expires (90 days
by default, 365 at most), and can be narrowed with **scopes**: SMART's `system/Task.rs`,
`system/AuditEvent.rs`, `system/Provenance.rs`, `system/CodeSystem.rs`, `system/NamingSystem.r`
or `system/*.rs` for FHIR reads, and `https://mendari.co/fhir/scope/decide` and
`.../outcome` for deciding and recording outcomes; deciding also lists the packs to decide with
(`GET /api/packs`, `GET /cds-services`). A scope only narrows a role. Each documented integration
path has a key preset with the role and scope it needs (`"preset": "cds-hooks"`, …;
`GET /api/auth/keys/presets`). The dashboard's **Access** view does the same for an admin who
signed in: it makes a key from a preset and shows it once, rotates and revokes keys, and shows
each one's history (`GET /api/auth/keys/{id}/history`).

Machines that speak **SMART Backend Services** (EHRs, integration engines, Da Vinci clients) can
get five-minute tokens instead, when a deployment turns it on (`MENDARI_BACKEND_SERVICES=on`;
it needs sign-in and `MENDARI_PUBLIC_URL`). An admin registers the client's public keys
(`POST /api/auth/clients`); the client signs an assertion (RS384 or ES384) and exchanges it at
`POST /auth/token`; `/fhir/.well-known/smart-configuration` describes it. A client's public keys
are replaced in place (`POST /api/auth/clients/{id}/keys`: same id, its tokens dropped), also
from the Access view's Clients tab, which takes a PEM public key and refuses the private keys it
recognises before sending anything. See
[ADR-0004](https://github.com/Junebugg1214/mendari/blob/main/docs/decisions/ADR-0004-machine-access-tokens-and-scopes.md) and
[deploying with real patient data](https://github.com/Junebugg1214/mendari/blob/main/docs/deploying-with-phi.md).

Every route has a permission, and anything unlisted needs admin:

| Role | May |
|---|---|
| viewer | read the dashboard and API |
| reviewer | also decide (Try it), resolve reviews, record outcomes, give feedback |
| service | an integration: read, decide, record outcomes |
| governance | read, approve automation |
| admin | everything: evaluations, exports, chain verification; with a signed-in person, API keys, clients, members and invitations |

With sign-in on, reviews, ratings, approvals and outcomes are recorded for whoever is signed in,
each new decision names who asked for it (`requested_by`, the requestor in the FHIR exports),
and the dashboard shows them as the audit chain names them and offers only what their role may
do. The chain names a person by their verified email, else by their identity provider's subject
qualified by its issuer (`<issuer>|<subject>`), never by a display name; a key as `key <id>`
and a Backend Services client as `client <id>`. In open mode the client names the reviewer,
reporter and approver, and each must be an identifier (no spaces, at most 100 characters). `MENDARI_OIDC_ORG_CLAIM` names the claim
that carries the person's organization: one deployment can serve several, and each sees only
its own decisions, reviews, feedback and reports.

**Memberships mode** (`MENDARI_ACCOUNTS=memberships`; the default is `claims`, all of the
above) takes each person's organization and roles from records instead of claims: an
organization is a record, and a person holds a membership of it with a set of roles, read on
every request ([ADR-0008](https://github.com/Junebugg1214/mendari/blob/main/docs/decisions/ADR-0008-accounts-and-memberships.md)).
The role and organization claims are ignored. A person with no membership acts for no
organization, never `default`: they may ask to join one or accept an invitation made for their
verified address, and nothing else. The operator manages organizations with `mendari access`,
and each organization's administrators invite and manage their own people in the dashboard's
Access view (beside its keys and clients) or through the API; an invitation's link opens
`/invite`, where the person signs in and accepts it
([the runbook](https://github.com/Junebugg1214/mendari/blob/main/docs/operations/accounts.md),
which also says how to switch a deployment).

A link or form on another site cannot act with a signed-in browser, or sign it out: such a
request reaches only public routes and pages, with GET or HEAD (a `403` otherwise). If a proxy serves Mendari under a
name that is neither the request's host nor `MENDARI_PUBLIC_URL`, list it in
`MENDARI_TRUSTED_ORIGINS` (comma-separated origins).

**Governance approvals.** At risk level 2 or 3 a person reviews every decision (ADR-0002) until
the organization approves automation. Someone with the `governance` role gives the approval
from the Decision Card, with a reference such as committee minutes and an expiry. It covers one
exact pack version: a new digest needs a new approval. It is written into every decision it
affects, chained in the audit log, and can be revoked at once. Level 4 can't be approved.

**Monitoring.** The dashboard's Monitoring view follows a pack in use, for the organization you
act for. It shows observed accuracy against the benchmark (from reviews, downstream outcomes and
thumbs-downs), the human override trend, confidence drift (PSI against the benchmark's
confidences), the automated share and cost per decision, week by week. Alerts fire only on
enough evidence:

- accuracy below the benchmark by more than the pack's tolerance;
- drift of 0.25 or more;
- an automated decision rated wrong;
- an override rate that doubled.

For a scheduler (cron, a Kubernetes CronJob), `mendari monitor` exits 1 on any alert (or on an
error, such as a `--db` it cannot read: the line on stderr says which) and
posts them to `--webhook` / `MENDARI_ALERT_WEBHOOK` (https only; keep the URL as a secret,
since most carry their token in the path):

<!-- check: skip placeholder: posts alerts to a webhook you supply -->
```bash
mendari monitor --db "$DATABASE_URL" --org clinic-a --webhook https://alerts.example.org/hook
```

**PHI mode.** `MENDARI_PHI_MODE=hash` keeps no input at all, only its hash.
`MENDARI_PHI_MODE=encrypt` keeps inputs encrypted (AES-256-GCM, `MENDARI_PHI_KEY`) apart
from the audit chain. Only signed-in reviewers and admins read them, and each read is chained
(and logged).
Both modes need `MENDARI_INPUT_HASH_KEY` (32 random bytes, base64, not the PHI key): the input
hash becomes an HMAC (`hmac-sha256:<key id>:<hex>`) that guessing cannot match, and the engine
refuses to start without it. In both, the chained record keeps no evidence values (sealed with
the input under `encrypt`) and names a resource in an input issue by type and position
(`Patient (entry 2)`), not by its id; the caller's own answer is unchanged.
Off Vercel, with sign-in or the EHR launch on, the engine refuses to start until a mode is
chosen; on Vercel the mode is `encrypt` unless set to `hash`, and `off` is refused. A database
once kept under `hash` or `encrypt` never takes plaintext again. `mendari phi purge` applies `MENDARI_RETENTION_DAYS` (default 30) without breaking the
chain; on Vercel a daily cron runs it once `CRON_SECRET` is set, and `mendari phi erase` erases
one decision's kept input on request. The PHI key rotates without losing anything
(`MENDARI_PHI_READ_KEYS`, then `mendari phi rotate`), the input hash key starts a new epoch, and
API keys rotate with an overlap. Notes (review
and rating notes, a payer's answer to a prior authorization) follow inputs: they are kept apart
from the chain, which holds only a salted commitment to each, sealed under `encrypt` and refused
under `hash`. Retention erases them too, `mendari notes erase` erases one at once, and
`mendari audit verify --notes` checks the kept ones. Read
[`docs/deploying-with-phi.md`](https://github.com/Junebugg1214/mendari/blob/main/docs/deploying-with-phi.md) before any real patient data.

**Deployments.** Each server records which pack version it runs, with which providers, in
which environment (`MENDARI_ENVIRONMENT`) and release, whenever that changes. The Decision
Card lists them (`/api/deployments`).

**The pilot, end to end.** `mendari sandbox pilot-check --engine URL` plays a pilot
organization against an engine with sign-in and PHI mode `encrypt`. People sign in through the
real OpenID Connect flow, an admin creates a key for the message worker, and the worker
decides (every input hash keyed). A reviewer reads the queue (each input read is logged) and
resolves; a viewer can't. The AI committee approves a level 2 pack, then monitoring and the
audit chain are checked. CI runs it against
[mock-oauth2-server](https://github.com/navikt/mock-oauth2-server) (job `pilot-check`).

## Decision Packs

A pack is a directory:

```text
packs/patient-inbox-router/
├── pack.yaml              # identity, risk, card, questions, provider slots, evaluation config
├── policy.yaml            # thresholds, rules, per-label limits, fallback
├── schemas/               # input JSON Schema (the output schema is generated)
├── fhir/mapping.yaml      # FHIR R4 → input, decision → Task
├── logic/                 # deterministic rules (red flags, keyword routing)
├── models/                # trained, calibrated local model + training provenance
├── data/                  # synthetic train / calibration / test splits
├── tests/cases.yaml       # behaviour tests every variant must pass
├── evals/benchmark.json   # published evaluation report, its ledger in evals/ledgers/
├── DECISION_CARD.md       # generated by `mendari card`
└── CHANGELOG.md
```

The core idea is that **a pack declares questions, not models.** Each question has a fixed set
of labels with human-readable criteria. Providers answer questions with a probability
distribution. The criteria text documents the pack for clinicians and is also, verbatim, the
instruction that Jev or an LLM receives. The policy then turns answers into an outcome:

1. **Rules first.** For example, `urgency=urgent` (p ≥ 0.35) becomes `HUMAN_REVIEW` → nurse,
   priority `stat`.
2. **Thresholds** on the primary question: AUTO ≥ 0.90 (≥ 0.95 for non-clinical
   destinations), review ≥ 0.50, abstain below.
3. **Caps that only reduce automation:** per-label limits, any provider failure, a provider the
   policy requires not running (`automation_requires`: the inbox, referral and AI Guard screens
   and the prior-auth notes must be the keyword rules plus a model, `rules_and_jev` or
   `rules_and_llm`), and the risk level.

### Risk levels are enforced by the runtime

| Level | Kind | Automation |
|---|---|---|
| 0 | Administrative (scheduling, routing, billing) | AUTO allowed by policy |
| 1 | Clinical information processing | AUTO allowed; audited |
| 2 | Clinical support | Human review by default; AUTO needs a recorded `GovernanceOverride` |
| 3 | Patient-specific prediction | As level 2, with stronger validation expected |
| 4 | Time-critical / diagnostic / treatment | Never autonomous |

A deploying organization lifts the level 2–3 cap with a named approver and a reference, for
example `GovernanceOverride(approved_by="CMIO", reference="AI-committee-2026-09")`. Every
affected audit record then carries it.

### Evaluation is part of the product

`mendari eval` reports accuracy, per-label precision/recall/F1, calibration (ECE, Brier,
reliability bins), outcome rates, abstention, false automation rate, expected human override
rate, critical-label safety (for example "urgent cases automated: 0"), latency, cost, subgroup
performance and a threshold sweep. Each proportion is k of n with its exact Clopper-Pearson
interval, and each Brier score has a seeded bootstrap interval. A run keeps a per-case ledger
beside its report (`--out`: `ledgers/<hash>.jsonl`), and the report's result is computed only
from it, so a second run gives the same bytes and anyone holding the ledger can check it. Neither
holds case text or case ids: a case is a keyed hash only the dataset's holder can match
([the formats](https://github.com/Junebugg1214/mendari/blob/main/docs/spec/evaluation-reports.md)). What a site shares (`--share`) holds none of
it, nor subgroups: a closed format, `mendari.site-report/1`, of names and rounded numbers only
(`--share-out FILE` writes it to read first).

`mendari compare A B --dataset X` compares two runs (two configurations, or two versions of a
pack) case by case from their ledgers: each figure in each run with its interval, the cases where
they differ, an exact McNemar test, Holm-adjusted, and the cases that changed.

`mendari leaderboard` compares provider configurations on the same records and publishes
the result in `evals/leaderboard.json` and the Decision Card. Configurations whose credentials
exist only on a deployment (such as Jev through Vercel Connect) run through the deployment's
budgeted evaluation endpoint, and their reports are merged in with `--report`:

<!-- check: skip secrets: calls an LLM and a Vercel deployment with your credentials -->
```bash
uv run mendari leaderboard mendari/patient-inbox-router --dataset test --limit 100 \
  --dataset red_flags --dataset red_flags_holdout \
  --run router=naive_bayes,safety_screen=rules --run router=llm,safety_screen=rules_and_llm
vercel curl /api/packs/mendari/patient-inbox-router/evals --deployment <url> -- -s -X POST \
  -H 'content-type: application/json' \
  -d '{"dataset": "test", "limit": 100, "providers": {"router": "jev"}}' > jev-test.json
uv run mendari leaderboard mendari/patient-inbox-router --report jev-test.json
```

## Feedback loop

Anyone using the dashboard can rate any decision, automated ones included, as right (thumbs
up) or wrong (thumbs down), with an optional note. A thumbs-down can also say what the answer
should have been, for the decision itself or for one of its checks
(`POST /api/audit/{id}/feedback` with `"expected": {"question": ..., "label": ...}`); the label
must be one the decision could have given. Each rating is appended to the decision's
hash-chained audit trail; its note is kept apart from it, so it can be erased. A person can change their rating: their latest one counts, and the
audit trail keeps both (people are told apart by reporter name; unnamed ratings of a decision
count as one person). The **Feedback** view lists the thumbs-down cases, and a thumbs-down on an
AUTO decision counts as "confidently wrong".

The system never retrains or rewrites itself from ratings. Instead, the thumbs-down cases are
downloaded as a dataset:

<!-- check: run server-db -->
```bash
mendari feedback export mendari/patient-inbox-router --db "$DATABASE_URL" --out down.jsonl
```

Records whose raters gave the right answer (and agree) already carry it in `labels`, so they are
ready-made test cases; fill in the others' `labels`. The file then works with
`mendari eval --dataset`, and confirmed misses become behaviour tests in the pack's
`tests/cases.yaml`. Improvements ship as a new, reviewed and versioned pack.

## Jev

Jev answers bounded, typed choice questions ("which team?", "is documentation missing?",
"does this need review?") and returns a probability for every label. That makes it a natural
decision primitive for packs. The adapter:

- maps pack questions 1:1 onto Jev questions (`choice`; `boolean` → `noul`; `score` → `score`,
  at most 10 levels), with every Jev question of a decision in one call;
- sends only `state_fields` (minimum necessary), over HTTPS;
- uses the chosen label's probability as the answer's confidence, like every other provider.
  Jev's own `confidence` summarises the shape of the whole distribution, so it is kept as
  evidence (`jev confidence 0.81`) rather than compared with policy thresholds;
- validates responses strictly: model match, label sets, probabilities sum to 1, choice is the
  argmax, token usage bounds, 64 KB cap. A malformed answer fails only the question it answers;
- retries once after `429` or `529 Overloaded` (a short backoff or a short `Retry-After`);
- records cost at the published price, $0.042 per million input tokens (output tokens are free);
- turns every failure (`not_configured`, `timeout`, `rate_limited`,
  `credential_unavailable`, `budget_exhausted`, `invalid_response`, …) into a safe policy
  outcome, never an automated guess.

`jev-latest` is an alias (currently `jev-1.13.0`). Every answer records the version that
actually answered; pin a version in `JEV_MODEL` when thresholds have been tuned against it.
Jev's documented weak spots for 1.13 (docs.typesafe.ai, "Jev 1.13 jaggedness") matter for pack
authors: it reads instructions literally, it is hurt by instructions that contradict the
criteria, adversarial text in the input can move its answers, and English is its strongest
language.

The endpoint and model are deployment settings, not part of any pack. The variable names are
fixed: a pack that tries to name others (`endpoint_env`, `api_key_env` and the like) is refused
at load, so no pack can choose which secret is sent, or where.

| Variable | Purpose |
|---|---|
| `JEV_API_URL` | Endpoint to call (required) |
| `JEV_MODEL` | Model id (required; recorded in every answer's provenance) |
| `JEV_CONNECTOR` | Vercel Connect connector that supplies the key, e.g. `jev/my-project` |
| `JEV_API_KEY` | Key for local use instead of a connector |
| `MENDARI_JEV_DAILY_LIMIT` | Daily call budget (default 500; `none` for no limit) |

<!-- check: skip secrets: calls Jev with your credentials -->
```bash
JEV_API_URL=... JEV_MODEL=... JEV_API_KEY=... uv run mendari eval mendari/patient-inbox-router \
  --dataset test --provider safety_screen=rules_and_jev --provider router=cascade
```

**With Jev set up, a server runs Mendari + Jev.** When `JEV_API_URL`, `JEV_MODEL` and a key
(`JEV_API_KEY`, or `JEV_CONNECTOR` on Vercel) are set, `mendari serve` and the Vercel app run the
reference packs that automate under their own policy with Jev beside the keyword rules
(`settings.JEV_DEFAULTS`), in every slot their provider settings choose nothing for:

| Pack | Slots |
|---|---|
| Patient Inbox Router | `safety_screen=rules_and_jev`, `router=cascade` (naive Bayes, and Jev when it is unsure) |
| Referral Router | `triage_screen=rules_and_jev`, `specialty_reader=cascade` (naive Bayes, and Jev when it is unsure) |
| Prior Auth Preflight | `notes=rules_and_jev` |

Their policies automate only while the screen is the keyword rules plus a model
(`automation_requires`), so a possible emergency is still flagged when Jev cannot answer. The
routers ask Jev only when naive Bayes is unsure, and keep naive Bayes' answer when Jev cannot
give one. The Clinical Trial Prescreener and AI Guard are not on the list: at risk
level 2 automation rests on an approval of the configuration that runs, so name their readers
yourself (`mendari/clinical-trial-prescreener:notes=jev`, `mendari/ai-guard:screen=rules_and_llm`).

- `MENDARI_PROVIDERS` decides every slot it applies to, e.g. `notes=rules` keeps the
  prior-auth notes on site (and its policy then automates nothing). `mendari serve --provider`
  replaces `MENDARI_PROVIDERS`, as before; the defaults fill the slots it leaves.
- When Jev fails or is over its daily budget, nothing is automated, and every case still gets
  the team or specialty naive Bayes suggests. The budget
  (`MENDARI_JEV_DAILY_LIMIT`, default 500) is shared by the three packs and by the dashboard's
  evaluations (up to 200 calls a run): size it for their daily volume.
- Commands that decide or evaluate (`decide`, `test`, `eval`, `leaderboard`, `demo`, the
  reference-pack build) never use these defaults. A server's own evaluations (the dashboard's,
  `POST /api/packs/…/evals`) run its deployed providers, Jev included.
- `/api/health` says whether the defaults are on (`jev.defaults`), which variant every slot
  runs (`providers`), and which slots run no model at all although their pack offers one
  (`without_model`); the dashboard's overview says so too. A screen with a model but without the
  rules (`safety_screen=jev`) is not listed, but it does not meet `automation_requires` either:
  each decision's reasons name what is missing. That Jev answers shows in `jev.calls_today` and
  in the decisions' errors.
- Upgrading a deployment that already had Jev set up for some slots: it now also runs Jev in
  the slots above that its settings leave. Name a slot to keep it as it was.

## LLMs and local models

The `llm` provider asks a chat model for a probability for every label, through any
OpenAI-compatible API. By default it calls the **Vercel AI Gateway**, which serves Claude,
open-weight models such as `openai/gpt-oss-120b`, and others behind one API. On Vercel it
authenticates with the deployment's OIDC token, so no key is stored. Point `LLM_BASE_URL` at
Ollama, vLLM or llama.cpp to run an open-source model on your own hardware instead.

- A JSON schema fixes the answer's shape: one probability per label per question. Anything
  else is an `invalid_response`, and so are probabilities that don't sum to about 1.
- Input text is data. The pack's questions and criteria form the system prompt; the input is
  sent as JSON, and the model is told to classify it, not to follow it.
- Verbalised probabilities are not logprobs. `mendari calibrate` fits a temperature per
  question on the pack's calibration split (live calls), and the evaluation report shows ECE.
- Cost per decision is the gateway's reported cost, recorded in every audit record.
- Every answer records the model the endpoint reports having answered with (a dated snapshot
  behind an alias, say) when it reports a well-formed id, and the configured model otherwise.

| Variable | Purpose |
|---|---|
| `LLM_MODEL` | Default model for `llm` variants (default `anthropic/claude-sonnet-5`; a variant's `model` wins) |
| `LLM_BASE_URL` | OpenAI-compatible endpoint instead of the AI Gateway, e.g. `http://localhost:11434/v1` |
| `AI_GATEWAY_API_KEY` | Gateway key for local use (on Vercel, the OIDC token is used) |
| `LLM_API_KEY` | Key for a non-gateway endpoint, if it needs one |
| `MENDARI_LLM_DAILY_LIMIT` | Daily call budget (default 500; `none` for no limit) |

As for Jev, these names are fixed, and an `llm` variant that names other variables
(`base_url_env`, `api_key_env`, `local_key_env`, `model_env`) is refused at load.

<!-- check: skip secrets: needs your Vercel project and its credentials -->
```bash
vercel env pull .env.local   # a fresh OIDC token for local calls through the gateway
vercel env run -- uv run mendari calibrate <pack> --slot router --variant llm --limit 100
```

## Deploying to Vercel

The whole engine (API, FHIR endpoints, CDS Hooks, review queue, audit, dashboard) deploys as one
FastAPI function: Vercel loads the root `app.py`, which serves `mendari.server.vercel:app`.

1. Connect a Postgres database (Neon via the Vercel Marketplace) for Production and Preview.
   It provides `DATABASE_URL`. On Vercel the app refuses to start without it, because functions
   have no persistent disk.
2. Set `MENDARI_PHI_KEY` and `MENDARI_INPUT_HASH_KEY` for Production and Preview: two different
   keys, each 32 random bytes in base64
   (`python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`), kept in
   your secret manager too. On Vercel the PHI mode is `encrypt` unless you set
   `MENDARI_PHI_MODE=hash`, and the app refuses to start without its keys or with `off`. Set
   `CRON_SECRET` (16 random characters or more) for Production: the daily retention purge in
   `vercel.json` runs only with it ([`docs/deploying-with-phi.md`](https://github.com/Junebugg1214/mendari/blob/main/docs/deploying-with-phi.md), section 3).
3. Attach the Jev connector to the project and set `JEV_CONNECTOR`, `JEV_API_URL` and
   `JEV_MODEL`: the reference packs then run Mendari + Jev ([Jev](#jev)). The key is fetched at
   runtime with the deployment's OIDC identity; it is never stored in the project's environment
   variables. `MENDARI_PROVIDERS` changes any slot, for every pack or scoped to one
   (`mendari/ai-guard:screen=rules_and_llm`); a setting a pack cannot use is skipped and listed
   in `/api/health`. An unscoped setting, or a Jev default, never applies to a community pack:
   only a setting that names it does.
4. Turn on sign-in for Production ([Sign-in for organizations](#sign-in-for-organizations);
   the project uses Google, set for Production only). Until the launch, also set Vercel
   Authentication to protect **all deployments**: previews run without sign-in, and the default
   setting leaves the production domain public. After each deploy, check that an
   unauthenticated request to production gets redirected to login.
5. Push a branch to get a preview deployment, check it with
   `vercel curl /api/health --deployment <url>`, then merge to `main` for production.

**Limits and spending.** With sign-in on, the app's rate limits are on
(`MENDARI_RATE_LIMITS`, see [`docs/deploying-with-phi.md`](https://github.com/Junebugg1214/mendari/blob/main/docs/deploying-with-phi.md)), and the
client address is Vercel's `X-Forwarded-For`. The edge layer is one Vercel firewall rule per IP
on `/auth/` and `/smart/`, kept as a file with its runbook in
[`deploy/vercel/`](https://github.com/Junebugg1214/mendari/blob/main/deploy/vercel/README.md); it starts in log mode. The launch goes in this
order: the external security review and its fixes; finite daily ceilings and quotas
(`MENDARI_JEV_DAILY_LIMIT=2000`, `MENDARI_LLM_DAILY_LIMIT=200` and
`MENDARI_ORG_DAILY_LIMITS=decisions=5000`); the firewall rule enforced after 7 days in log
mode; then Vercel Authentication lifted.

**Logs.** The security log is the function's stdout, in Vercel's runtime logs (find a request by
the `X-Request-Id` a user quotes; on Vercel it is the `x-vercel-id`). Vercel keeps them for a
day on Pro (an hour on Hobby); a log drain keeps them longer.

`/api/health` reports the storage backend, the packs (and how many are maintained or
community), active providers, Jev configuration and today's Jev usage (never secrets). With sign-in on, only a principal that may read (someone signed in, or a
key) sees them; anyone else gets `status`, `version` and `sign_in`, enough for a load balancer.
On a preview without sign-in `vercel curl` shows everything; with sign-in on, open
`/api/health` in a browser signed in to the dashboard. The FHIR sandbox (HAPI) stays in local
`docker compose`.

One pack never stops the server. Its `invalid` lists the packs that failed to load (by path; an
error that is not Mendari's by its type alone), which are not served; the packs whose providers
could not be built (by id), which stay listed, counted in `packs` and offered in CDS Hooks
discovery, but whose decision and detail requests answer `422`; and the packs whose terminology
could not be built, which are served without it. Every other pack decides.

Days and times people see (the dashboard's times and daily volume, daily call counters, "today"
for criteria and ages, card dates) use `MENDARI_TIMEZONE`, an IANA name defaulting to
`America/New_York`. Stored timestamps, the audit hash chain, API JSON and FHIR stay UTC.

Preview and production deployments share the same Neon database unless Neon's preview branching
is enabled, so preview traffic appears in the production audit trail. With preview branching,
each preview gets a `preview/<git branch>` database branch. The Vercel-managed integration only
deletes it when Vercel deletes the deployment (months later), and a full branch limit makes
previews fail with "Resource provisioning failed". The `neon-preview-cleanup` workflow deletes
the branch when its pull request closes; it needs a project-scoped Neon API key as the
`NEON_API_KEY` repository secret and the Neon project id as the `NEON_PROJECT_ID` variable.

## Project layout

```text
src/mendari/
├── core/        spec, pack loading/digest, registry, runtime, policy, audit, Decision Card
├── providers/   rules, naive_bayes (local ML), jev
├── standards/   fhir (R4 mapping, Task output), cds_hooks
├── evals/       metrics, harness, local-vs-benchmark comparison, pack behaviour tests
├── server/      FastAPI app (REST, FHIR, CDS Hooks, review, audit, evals)
├── dashboard/   developer dashboard: HTML, CSS and ES modules, no dependencies or build step
└── sandbox/     synthetic data generator, FHIR seed/worker, demo
packs/           reference Decision Packs (maintained by Mendari)
community-packs/ community Decision Packs, contributed by pull request (MAINTAINERS.yaml)
datasets/        example "local site" dataset for validation
docs/            spec, guides, architecture, ADRs (the documentation site)
notes/           project notes: milestone plans, design and audit reviews, the build log
```

## Development

<!-- check: skip ci: each line is a step of CI jobs test, browser and reference-pack -->
```bash
uv sync
uv run pytest                          # unit and integration tests
uv run playwright install chromium     # once, for the dashboard's browser tests
uv run pytest -m browser               # the dashboard in a real browser (free: default providers)
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src tests
uv run python scripts/check_doc_snippets.py   # the API examples against a local server (curl, jq)
./scripts/build-reference-pack.sh      # regenerate data → train → test → benchmark → card
```

## Roadmap

Full plan and status: [`docs/roadmap.md`](https://github.com/Junebugg1214/mendari/blob/main/docs/roadmap.md). Done:
- v0.1 runtime and inbox pack;
- v0.2 model-agnostic core (LLMs, composition, leaderboards);
- v0.3 the other four packs;
- the dashboard redesign;
- v0.4 healthcare standards: US Core, AuditEvent/Provenance, SMART on FHIR, CDS Hooks fetch,
  Bulk FHIR;
- v0.5 the Hub: signed pack archives, `mendari push / pull`, a registry and website (built, not
  public yet);
- v0.6 readiness for organizations: sign-in with roles and API keys, tenancy, governance
  approvals, monitoring and drift, PHI mode, container images and on-prem templates;
- v0.7 later standards: a CQL provider, OMOP CDM inputs, Da Vinci CRD, DTR and PAS;
- v1.0, everything up to the launch: a security review with scans in CI, the pack-authoring
  guide and `mendari new`, the docs site, the contribution flow with its eval gate,
  community files, a release workflow and the 10-minute quickstart check.

Next: **the public launch**. The repository goes public, 0.1.0 goes to PyPI, and the Hub and
docs site go live. That is the owner's decision.

## License

Apache-2.0. See [LICENSE](https://github.com/Junebugg1214/mendari/blob/main/LICENSE), and
[NOTICE](https://github.com/Junebugg1214/mendari/blob/main/NOTICE) for the third-party material and
terminology notices. Changes: [CHANGELOG.md](https://github.com/Junebugg1214/mendari/blob/main/CHANGELOG.md).
