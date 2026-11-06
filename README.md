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
| Decision Pack specification | [`docs/spec/decision-pack-0.2.md`](https://github.com/mendari-hq/mendari/blob/main/docs/spec/decision-pack-0.2.md) (0.1: [`decision-pack-0.1.md`](https://github.com/mendari-hq/mendari/blob/main/docs/spec/decision-pack-0.1.md); 0.3, the FHIR mapping only: [`decision-pack-0.3.md`](https://github.com/mendari-hq/mendari/blob/main/docs/spec/decision-pack-0.3.md)), [`src/mendari/core/spec.py`](https://github.com/mendari-hq/mendari/blob/main/src/mendari/core/spec.py) |
| Mendari runtime | [`src/mendari/core/decision.py`](https://github.com/mendari-hq/mendari/blob/main/src/mendari/core/decision.py), [`policy.py`](https://github.com/mendari-hq/mendari/blob/main/src/mendari/core/policy.py) |
| Providers: rules, naive Bayes, Jev, LLMs (Vercel AI Gateway, or OpenAI-compatible local models), criteria, CQL, dosing; combined with `any_of`, `cascade`, `vote` | [`src/mendari/providers/`](https://github.com/mendari-hq/mendari/tree/main/src/mendari/providers) |
| FHIR adapter + CDS Hooks | [`src/mendari/standards/`](https://github.com/mendari-hq/mendari/tree/main/src/mendari/standards) |
| Patient Inbox Router reference pack | [`packs/patient-inbox-router/`](https://github.com/mendari-hq/mendari/tree/main/packs/patient-inbox-router) ([Decision Card](https://github.com/mendari-hq/mendari/blob/main/packs/patient-inbox-router/DECISION_CARD.md)) |
| Clinical Trial Prescreener reference pack (v0.3) | [`packs/clinical-trial-prescreener/`](https://github.com/mendari-hq/mendari/tree/main/packs/clinical-trial-prescreener) ([Decision Card](https://github.com/mendari-hq/mendari/blob/main/packs/clinical-trial-prescreener/DECISION_CARD.md)) |
| Referral Router reference pack (v0.3) | [`packs/referral-router/`](https://github.com/mendari-hq/mendari/tree/main/packs/referral-router) ([Decision Card](https://github.com/mendari-hq/mendari/blob/main/packs/referral-router/DECISION_CARD.md)) |
| Prior Authorization Preflight reference pack (v0.3) | [`packs/prior-auth-preflight/`](https://github.com/mendari-hq/mendari/tree/main/packs/prior-auth-preflight) ([Decision Card](https://github.com/mendari-hq/mendari/blob/main/packs/prior-auth-preflight/DECISION_CARD.md)) |
| AI Guard reference pack (v0.3) | [`packs/ai-guard/`](https://github.com/mendari-hq/mendari/tree/main/packs/ai-guard) ([Decision Card](https://github.com/mendari-hq/mendari/blob/main/packs/ai-guard/DECISION_CARD.md)) |
| Human review / abstention | policy outcomes + [`core/audit.py`](https://github.com/mendari-hq/mendari/blob/main/src/mendari/core/audit.py) review queue |
| Evaluation harness | [`src/mendari/evals/`](https://github.com/mendari-hq/mendari/tree/main/src/mendari/evals) |
| Audit trail | SQLite or Postgres, append-only hash-chained events, `mendari audit verify` |
| Synthetic healthcare dataset | [`src/mendari/sandbox/synthetic_inbox.py`](https://github.com/mendari-hq/mendari/blob/main/src/mendari/sandbox/synthetic_inbox.py), [`synthetic_trial.py`](https://github.com/mendari-hq/mendari/blob/main/src/mendari/sandbox/synthetic_trial.py), [`synthetic_referral.py`](https://github.com/mendari-hq/mendari/blob/main/src/mendari/sandbox/synthetic_referral.py), [`synthetic_prior_auth.py`](https://github.com/mendari-hq/mendari/blob/main/src/mendari/sandbox/synthetic_prior_auth.py), [`synthetic_guard.py`](https://github.com/mendari-hq/mendari/blob/main/src/mendari/sandbox/synthetic_guard.py) |
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
git clone https://github.com/mendari-hq/mendari.git
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
   [Contributing a community pack](https://github.com/mendari-hq/mendari/blob/main/docs/guides/contributing-a-pack.md),
   and what CI checks (`mendari community-check`) in
   [community-packs/README.md](https://github.com/mendari-hq/mendari/blob/main/community-packs/README.md).

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

No programming needed: a pack is YAML. The [authoring guide](https://github.com/mendari-hq/mendari/blob/main/docs/guides/authoring-a-pack.md)
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
uv run mendari scoreboard arms mendari/referral-router   # its configurations, by arm
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

A long chain is quicker to check from a checkpoint. `mendari audit checkpoint` verifies the chain
from its last checkpoint (from the start, the first time) and, when it holds, records its head as
the next one, in a row outside the chain. `--since-checkpoint` then reads only the events after
it. A full `mendari audit verify` also checks that every checkpoint still names its event, so a
chain rewritten from before one, or cut short of it, fails there (`reason: checkpoint`). A
checkpoint lives in the same database, so someone who can rewrite the chain can rewrite it too:
keep a copy of the head somewhere else as well. With `MENDARI_CHECKPOINT_KEY` set, each day's is
also signed (Ed25519) and a server publishes it at `GET /api/audit/checkpoint`: a copy anyone
keeps lets `mendari audit verify --against <statement> --public-key <key>` show later whether the
chain was rewritten from before it. Run it daily, beside retention:

<!-- check: run server-db -->
```bash
mendari audit checkpoint --db var/mendari.db
mendari audit verify --db var/mendari.db --since-checkpoint
```

**Errors and exit codes.** A command that cannot do what was asked prints one line per problem
(`error: ...`, naming the file, the field or the flag, and what to do) and exits 1, never a
traceback; `--debug` on any command (or `MENDARI_DEBUG=1`; `0` or `off` keeps it off) shows the
full traceback instead. Exit 1 is also a failed check (a failing behaviour test, a broken chain,
`monitor`'s alerts); `eval --fail-on-degradation` exits 2 on a regression; `monitor` exits 3
when its alerts could not be posted; Ctrl-C exits 130 (a
parallel `eval` stops sending the rest of the dataset); a closed pipe (`| head`) exits 141, with
no error line.

**The loop cannot pass by accident.** `mendari test` refuses a behaviour test whose `expect` is
missing, empty, misspelt or names what the pack can never decide, and fails when the pack has
no tests; `mendari validate` also reads every dataset the pack declares. A `--provider` (or
`leaderboard --run`) setting that applies to nothing in the pack is an error; from
`MENDARI_PROVIDERS`, which a deployment shares across packs, it is a warning (and, on a server,
reported in `/api/health` as `provider_settings_unused`).

**Read-only commands.** `audit verify`, `audit export`, `feedback export`, `monitor` and
`access check` (with `access org list`, `members`, `requests`, `keys` and `clients`) open the
server's database read-only: they never create, migrate or write it, and a `--db` that does not
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
with sign-in on. The full reference is `/docs/reference`, the [API guide](https://github.com/mendari-hq/mendari/blob/main/docs/guides/api.md) has
the same examples as whole scripts, and FHIR is under
[Healthcare standards](#healthcare-standards). Every `curl` example in this README (but `vercel
curl`, which goes through a deployment) is a step of the scripts in
[`src/mendari/dashboard/snippets/`](https://github.com/mendari-hq/mendari/tree/main/src/mendari/dashboard/snippets) (a test keeps them equal),
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
as tested; nothing is published before that. [`deploy/`](https://github.com/mendari-hq/mendari/tree/main/deploy) has
templates for a site's own infrastructure: docker compose on one server and Kubernetes. Both come
with Postgres, your identity provider, PHI mode and the daily retention, checkpoint and monitoring jobs.

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
server. No real PHI is involved. The engine runs open mode with `MENDARI_CDS_SANDBOX=1`, so its
CDS Hooks services answer the other containers and your browser, which Docker's port publishing
brings in from its network's gateway; an open-mode server without it answers them on its own
machine only.

A SMART EHR launch, scripted end to end (EHR and browser in one), decides a patient's referrals
or messages:

<!-- check: skip docker: needs the Docker sandbox; CI job sandbox-e2e runs it -->
```bash
docker compose exec engine mendari sandbox smart-launch --pack mendari/referral-router \
  --engine http://engine:8000 --launcher http://launcher --fhir http://fhir:8080/fhir
```

The profile `signin` adds a second engine with sign-in on (port 8001) where the launcher is
registered to decide for organization `pilot`; CI's `sandbox-e2e` checks that its launches
decide there, chained as the launcher's `<issuer>|<subject>`. Sign-in never runs PHI mode `off`,
so this engine runs `encrypt` with two keys of your own, generated once into `.env` (gitignored;
compose reads it, and a variable set in your shell wins); the first lines below add each key
only if `.env` lacks it. Keep them: inputs sealed under one pair do not open under another. Its
database, once protected, never takes `off` again, so to start over (new keys, or an older
compose file) remove this engine and its volume alone with
`docker compose --profile signin rm -sf engine-signin && docker volume rm mendari_mendari-signin-data`
(`down -v` would also remove the open engine's database and the local registry's packs).

<!-- check: skip docker: needs the Docker sandbox; CI job sandbox-e2e runs it -->
```bash
[ -n "$(tail -c1 .env 2>/dev/null)" ] && echo >> .env  # give a last line its newline
for name in MENDARI_PHI_KEY MENDARI_INPUT_HASH_KEY; do
  grep -qs "^$name=" .env ||
    echo "$name=$(python3 -c 'import os,base64;print(base64.b64encode(os.urandom(32)).decode())')" >> .env
done
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

The Referral Router tells a referral from other ServiceRequests by the code its FHIR mapping
lists (`focus_where` in `fhir/mapping.yaml`): `FR101`, the fictional code of its synthetic data,
since no package carries SNOMED CT. List the code your EHR marks referrals with first (often a
SNOMED CT code), or no referral is found. The same goes for the Clinical Trial Prescreener's
condition codes beyond ICD-10-CM (`logic/criteria.yaml`). Its mapping reads a Condition's first
coding only, so a Condition coded first in SNOMED CT is not found even when an ICD-10-CM coding
follows.

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
[ADR-0003](https://github.com/mendari-hq/mendari/blob/main/docs/decisions/ADR-0003-a-served-task-is-a-decision.md)). Each decision is a
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
is refused. A prefetch template is a read or search relative to that server (`Type/<id>`,
`Type/{{context.patientId}}` or `Type?<query>`): a community or registry pack with any other
(an absolute URL, `..`, an operation, a fragment) is refused at load, and your own pack is warned.
Providers that call a model have an allow-list of their own: `MENDARI_PROVIDER_ALLOWED_ORIGINS`
([LLMs and local models](#llms-and-local-models)).

List the CDS services, and call the inbox's hook with a synthetic patient and message as
prefetch (with sign-in on, `MENDARI_KEY` as in the [REST quickstart](#rest-api); in open mode,
on this machine):

<!-- check: snippet cds.sh/discover -->
```bash
curl -sS --fail-with-body "$MENDARI/cds-services" -H "Authorization: Bearer $MENDARI_KEY" \
  | jq '[.services[] | {id, hook}]'
```

<!-- check: snippet cds.sh/hook -->
```bash
curl -sS --fail-with-body -X POST "$MENDARI/cds-services/mendari.patient-inbox-router" \
  -H "Authorization: Bearer $MENDARI_KEY" -H 'content-type: application/json' \
  -d '{"hook": "mendari-message-received", "hookInstance": "7f1c2a4e-0d1b-4c55-9a51-3c1e2d000003",
    "context": {"patientId": "example-patient", "communicationId": "example-message"},
    "prefetch": {
      "patient": {
        "resourceType": "Patient", "id": "example-patient",
        "meta": {"tag": [{"system": "https://mendari.co/fhir/sandbox", "code": "synthetic"}]},
        "identifier": [{"system": "https://mendari.co/fhir/synthetic-case", "value": "1"}],
        "name": [{"family": "Synthetic", "given": ["Example"]}],
        "gender": "unknown", "birthDate": "1984-06-15"},
      "communication": {
        "resourceType": "Communication", "id": "example-message", "status": "in-progress",
        "subject": {"reference": "Patient/example-patient"}, "sent": "2026-09-29T13:00:00Z",
        "payload": [{"contentString": "Can I get a refill of my blood pressure medicine?"}]}}}' \
  | jq '.cards[] | {summary, indicator}'
```

**An EHR's own token (LP-23b).** With sign-in on, the CDS Hooks services take Mendari's own
credentials (a key made from the `cds-hooks` preset, or a SMART Backend Services token scoped to
decide) and, once you register an EHR in `MENDARI_CDS_CLIENTS`, the short-lived JWT that EHR
signs itself (CDS Hooks 2.0). A registered client names its `iss`, its organization, the FHIR
bases it may send as `fhirServer`, and its public keys, by value or from one `jwks_uri` on
`MENDARI_FHIR_ALLOWED_SERVERS`. A token must be RS384 or ES384, addressed (`aud`) to
`MENDARI_PUBLIC_URL` plus the path it calls, live at most 330 seconds, and carry a `jti` used
once. A verified token is the principal `cds:<id>` of the registered organization: it lists and
calls the CDS services and reaches nothing else. Unset, a JWT on `/cds-services` is refused in
every mode, open mode included, where before it was never read. Elsewhere a JWT is what it was:
refused with sign-in on, and ignored in open mode, which reads no credential there.

**Who may call from where (LP-23c).** With sign-in off, the CDS Hooks services answer this
machine only: a call from a loopback address, to a `Host` that names this machine, with no
forwarding header. From anywhere else every call is a `403`, before any credential is read, a
valid token included (the security log says `open_mode_remote`); the rest of open mode answers
any host, as before. `MENDARI_CDS_SANDBOX=1` lifts this for a synthetic sandbox reached over a
network: the compose sandbox's open engine sets it, and the server refuses to start with it on
Vercel or with sign-in on. A registered client whose pages call from a browser lists their
origins (`origins`). On `/cds-services` and its children only, a page there gets CORS: its
preflight is answered, its answers are readable, and never with credentials. Its client's token
is refused from a page on any other origin but Mendari's own, and a page on an origin nobody
registered is refused as before. How it works, field by field, is
[Connecting an EHR](https://github.com/mendari-hq/mendari/blob/main/docs/guides/connecting-an-ehr.md);
the decision, and what a hook is to do with a record it cannot read, is
[ADR-0014](https://github.com/mendari-hq/mendari/blob/main/docs/decisions/ADR-0014-a-cds-client-is-a-principal.md)
(Proposed). Where pilot readiness stands, criterion by criterion with the test that shows each,
is graded in [`notes/ehr-scorecard.md`](https://github.com/mendari-hq/mendari/blob/main/notes/ehr-scorecard.md).

**A record Mendari cannot read (LP-23d).** A hook always answers. Each prefetch key is read on its
own. A key the request leaves out (or sends empty), with no `fhirServer` to read it from, is a
`412`, as CDS Hooks 2.0 says (it was a `422`). A key whose value is null has no data and is not
read. Everything the client sends goes through the structural R4 check, as before (a `400`). When
a key cannot be read (a failed read, an OperationOutcome, a value that is not a resource, a
resource or search page Mendari read that is not R4 JSON, a search past the page cap), or a note
is only a link or its data does not decode, the decision is ABSTAIN before any provider runs. Its
reasons read `could not read <key>: <reason>`, with a reason from a closed set (`timeout`,
`unauthorized`, `not_fetched` and the others), never a URL, a query or what a server answered.
The hook answers `200`, the decision is recorded like any other, and its card is an `info` card
that names the keys, whatever the pack's `show` says. Input that is refused still BLOCKs, and a
request fault stays a `4xx` (a required resource the client sent something else for among them).
A Da Vinci CRD order whose Coverage was read gets `covered: indeterminate` for a `technical`
reason. The SMART launch's decide reads its record the same way. Every FHIR decision carries
`data_coverage` (`mendari.data-coverage/1`) in its record (`GET /api/audit/{id}`; the answers of
`fhir/decide` and the SMART launch's decide hold it too, while a card and `$decide` give the
decision id): for each mapped field and list, the paths it looked for, the code systems the pack
declares, the pack's own codes, and what it found, as counts (resources by type, values, codings
by declared system with any other system as one count, what could not be read by reason). It
never holds a value, an id, text, a patient's code or a system the pack does not declare. A JSON
decision carries none.

**Cards and feedback (LP-23f).** A card's `uuid` is its decision's id, and a card with its
suggestion says `selectionBehavior: at-most-one` and offers three override reasons (not
relevant, already addressed, clinical judgement) from Mendari's own CodeSystem. Its summary uses
the pack's display names and stays under 140 characters, checked when the pack loads; its detail
is the pack's status, the notice and one plain sentence, never a percentage or the confidence.
With `MENDARI_PUBLIC_URL` set, the reasons are behind a link to the decision in the audit trail,
and a launch-registered EHR of the same organization gets a `smart` link that opens the pack. The
prior authorization service declares Da Vinci CRD's version and its `coverage-info` option in
discovery, and honours `coverage-info: false`. The EHR may send back what the clinician did with
each card (`POST /cds-services/{id}/feedback`): each is chained as `card.feedback`, codes and ids
only, and the comment is never kept. A card from another organization, another pack or another
caller is one `404`. In the dashboard, a FHIR decision's record shows what it read, as counts.

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
missing. A questionnaire asked for by its canonical comes beside the orders' packages,
unanswered, and each questionnaire version comes once (an order's package holds its own). A
questionnaire that is not found here is a warning in `outcome`, beside any package; so is a
version other than the pack's, the only one served. An order or a `context` that names no
decision of the caller's organization is a warning too. Orders or a `context` that name no
decision, with nothing to return, are a `422` OperationOutcome (`not-found`): DTR asks for a
4xx. An unknown id and another organization's get the same answer, which names neither. A
decided order that needs no documentation gets an information note instead. The answers are
profile-validated against DTR 2.2.0 (HL7 validator, offline) in CI, which checks their
structure. How the operation behaves is checked by the Inferno DTR payer suite v0.18.0
(`dtr_payer_server_v220`, Inferno as the DTR client), weekly and by hand, against the engine on
synthetic data, with the gaps it lists: the CapabilityStatement claims `$questionnaire-package`
only (no `$next-question`, `$log-questionnaire-errors` or `ValueSet/$expand`), no question
depends on another's answer, answers are filled on the server rather than by CQL the EHR runs,
and some must-support elements are unused. Every test's expected result, with the reason for
each that is not a pass, is in
[`sandbox/inferno/`](https://github.com/mendari-hq/mendari/blob/main/sandbox/inferno/README.md),
with what the run cannot show: a payer's acceptance, an EHR's DTR client, X12 or CPT. The HL7
DTR reference app is never run against Mendari: it would send its requests to https://mendari.co.
A pack declares it with `cds_hooks.questionnaire`.

**Da Vinci PAS.** For a complete request (`submit_ready`), `POST /api/packs/{pack}/pas` (or
`mendari pas build`) builds the PAS request Bundle. It holds a `preauthorization` Claim, the
patient, Coverage, payer, requesting organization and order from the EHR, the place of service,
and the X12 codes PAS asks for. Nothing is filled in: a missing element is an error that names it.
Mendari ships no X12 code value (X12's code lists are under X12's terms): each comes from the
request (`request_type`, `certification_type`, `service_type`; flags `--request-type` and so on),
else the order (the service type), else the site's settings `MENDARI_PAS_REQUEST_TYPE`,
`MENDARI_PAS_CERTIFICATION_TYPE` and `MENDARI_PAS_SERVICE_TYPE`. Those three are required, and
the error names the setting. The codes PAS makes optional are sent only when set:
`MENDARI_PAS_SELF_RELATIONSHIP` (the Coverage's code for self), `MENDARI_PAS_PAYER_ENTITY` and
`MENDARI_PAS_REQUESTER_ENTITY` (the organizations' entity codes).
With `submit`, a decision that was automated, or that a reviewer resolved as `submit_ready`,
goes to the payer's `Claim/$submit` (`MENDARI_PAS_PAYER_URL`, allow-listed),
and the payer's decision is chained on the decision as codes: its outcome and review actions
(text in their place is dropped). Its authorization number and disposition are kept as a note. The sandbox has a mock payer (`mendari sandbox payer`, compose service `payer`). It
answers with the review action code `MENDARI_SANDBOX_PAYER_REVIEW_ACTION` gives, or with none,
which PAS allows; the compose sandbox sets its own codes, all but the service type, which each
request there gives (`service_type`).
The request and the payer's answer validate against PAS 2.2.1 in CI, with every X12 code a site
may set and with only those PAS requires.

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
fail it. The validator loads only the FHIR packages `scripts/fhir_packages.lock` pins, each
downloaded from the FHIR package registries and checked against its sha256 (`sha256sum -c`), and
runs with no HTTP access, so its verdict depends on nothing fetched by version alone. Each run
has a package cache of its own with only what it loads, so the US Core run checks against US
Core 6.1.0 and no other version.
`scripts/fhir_packages.py lock` writes the lock again after a validator or guide version changes.

## Documentation

Every page in [`docs/`](https://github.com/mendari-hq/mendari/tree/main/docs) builds into a documentation site with MkDocs Material:

<!-- check: skip long-running: mkdocs serve runs until stopped; CI job docs builds the same site strictly -->
```bash
uv sync --group docs
uv run mkdocs serve    # http://127.0.0.1:8000
```

CI builds it in strict mode, so a broken link fails. It will be hosted once the repository is
public. Until then a running server documents itself: `/docs` is the developer landing (a
quickstart in three calls, FHIR, authentication), and `/docs/reference` the full API reference.
The [API guide](https://github.com/mendari-hq/mendari/blob/main/docs/guides/api.md) covers the same ground in the site.
To contribute a pack, read
[Contributing a community pack](https://github.com/mendari-hq/mendari/blob/main/docs/guides/contributing-a-pack.md);
why community packs work as they do is
[ADR-0007](https://github.com/mendari-hq/mendari/blob/main/docs/decisions/ADR-0007-community-packs-by-pull-request.md).

## Security

[`SECURITY.md`](https://github.com/mendari-hq/mendari/blob/main/SECURITY.md) says how to report a vulnerability and how to deploy safely. The
threat model ([`docs/security/threat-model.md`](https://github.com/mendari-hq/mendari/blob/main/docs/security/threat-model.md)) lists every
threat with its mitigation and the test that checks it. On every change, the CI job `security`
audits the Python and Hub-site dependencies and runs an OWASP ZAP scan of the API with sign-in
on, and the job `hub-site` runs the ZAP baseline over every Hub page; GitHub Actions are pinned
by commit and images by digest. Every response carries a strict
CSP and the usual isolation headers (HSTS off localhost), the developer landing and the API
reference included: the reference is Swagger UI served from vendored, pinned files, never a CDN,
and the landing loads nothing but its own files. Sessions end after 30 minutes idle
and 8 hours at most, behind `__Host-` cookies. A browser request that another site starts
reaches only public routes and pages, to read. A request
body is 4 MiB at most (`MENDARI_MAX_BODY_BYTES`; a `413` otherwise), and public routes take no
lock and do little work for a caller without a credential. A decision's remote calls share one
deadline (`MENDARI_DECISION_DEADLINE_S`, 120 s unless set, 10 to 270): past it a call is not
made, one under way is cut, and the decision records `timeout` and goes to a person, while its
local providers still answer. With sign-in on, decisions, the AI
guard, evaluations and exports are rate limited per caller, and sign-in and the token endpoint per
address, in the database so every instance shares the count (`MENDARI_RATE_LIMITS`; a `429` with
`Retry-After`); organizations can have daily quotas (`MENDARI_ORG_DAILY_LIMITS`), over which
decisions fall back to human review (their evaluations have their own, `eval_jev` and
`eval_llm`), and each principal (a person, a key, a client, an EHR
launch) a daily cap on the decisions it asks for (`MENDARI_PRINCIPAL_DAILY_LIMIT`), past which its
requests are refused until midnight (`429`), so one leaked key cannot spend its organization's day. A security log goes to stdout as JSON lines: one access line
per API request and the security events (sign-ins and EHR launches, refusals, requests from other sites, rate
limits, key, client and governance changes, exports, evaluations, input and note reads), from an
allow-list of fields that has no room for a credential, a query value, a body or case text.
Every response carries an `X-Request-Id`, which JSON errors on `/api` repeat as `request_id`.
Under PHI mode the input hash is keyed and notes stay out of the audit chain (see PHI mode
below). Every security setting, with its default, is in
[deploying with real patient data](https://github.com/mendari-hq/mendari/blob/main/docs/deploying-with-phi.md#8-settings-at-a-glance).

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
mendari yank my-site/my-pack@1.0.0 --reason "misroutes refills" --severity high --key keys/publisher.key
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
- **Trust.** A pulled pack runs on the engine for packs the deployment does not own, as a
  community pack's patterns do: RE2, which never backtracks, within caps, 10,000 characters of
  input and a CPU budget per decision, after which a person decides. A copy of a reference pack
  of this release keeps the maintained engine (`mendari pull` says which), chosen by the digest
  of its files, never by its manifest or the registry
  ([ADR-0009](https://github.com/mendari-hq/mendari/blob/main/docs/decisions/ADR-0009-bounded-evaluation-for-packs-the-deployment-does-not-own.md)).
  Served, a pulled pack never automates until your organization approves its exact digest, on
  its Decision Card or with `POST /api/registry-approvals` (chained; `DELETE` revokes it), at
  any risk level, and never while the registry recalled it; nothing in the pack lifts that. A
  running server reads a registry's recalls again when a decision for one of its packs finds the
  last read five minutes old (that decision uses the list read before), and holds a recalled
  pack the same way, and one whose registry it could not read for a day; its full `/api/health`
  shows each registry's last read under `registry_recalls`. `mendari test` and `mendari eval`
  check the pack's own policy, and neither `mendari eval`, `mendari demo`, the evaluation route
  nor the Monitoring view compares a pulled pack with the benchmark it carries (its publisher's
  upload). The verified marker beside a pulled pack records the registry, namespace, publisher
  type and key it came from
  ([ADR-0012](https://github.com/mendari-hq/mendari/blob/main/docs/decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)).
  The packs you own (the reference packs, and your own in `MENDARI_PACKS_PATH`, where a copy of
  a pulled pack you trust goes) run on Python `re` with no time limit: installing one means
  trusting its author, as with any plugin. What a pack cannot do (choose secrets or endpoints,
  reach outside its folder, stop the server, reach another pack's CQL) is in
  [SECURITY.md](https://github.com/mendari-hq/mendari/blob/main/SECURITY.md#pack-supply-chain).
- **The registry** (`mendari registry serve`) has namespaces with a publisher type
  (community, verified, institutional) that its admins set after verification. `--share` sends
  a site's metrics, never its cases or their ids, in a closed format the registry checks
  (`mendari.site-report/1`); the registry lists them as unverified until sites sign them.
- **What the registry shows** is only what it checked
  ([ADR-0012](https://github.com/mendari-hq/mendari/blob/main/docs/decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)).
  It renders each version's Decision Card itself, from the files the pack digest covers, and
  never shows the card, benchmark or leaderboard a publisher uploads (they stay in the signed
  archive, unread). Every version answers two groups: `checked` (the signing key, the archive's
  SHA-256, the digest, the engine, the card rendered here, the review status) and `claimed`
  (the publisher, the risk level it declares, the licence and summary). Every registry write
  is a hash-chained event (`mendari registry verify --db …` checks the chain and that every
  namespace, key, token, version, report, review, recall and deletion is the row its event
  hashed; keep `GET /v1/chain/head` elsewhere, since the chain is unkeyed), and a registry
  reviewer's review (`POST /v1/packs/{namespace}/{name}/{version}/review`, the admin token
  until LP-19) records the risk level it found beside the one the publisher declared.
- **A recall** (`mendari yank`) is a signed record with a reason and a severity: the
  publisher signs it with a namespace key, or the registry with its own
  (`MENDARI_REGISTRY_SIGNING_KEY`, published at `GET /v1/registry-key`); `GET /v1/yanks` lists
  every recall. A recalled version is never the latest and is not
  searched, and is still pulled by its exact version, with the record shown and its signature
  checked against the keys you pinned. An administrator can also delete a version that must
  not be served: its archive's bytes go, and a tombstone stays.
- **The regression check.** On every publish the registry evaluates the version on its own
  test set, in an isolated child process with local providers only, and compares it with the
  earlier version (the pack gate); it shows the result as "publisher's own test set:
  regression check, not a ranking" and never refuses a version for it
  (`MENDARI_REGISTRY_REGRESSION=off` turns it off).
- **Limits and headers.** The registry limits reads per address and writes per token
  (`MENDARI_REGISTRY_RATE_LIMITS`: `on` unless set, `log` or `off`, then overrides such as
  `registry_write=240/h:40`), and both it and the Hub send a strict CSP, refuse framing and send
  HSTS.
- **The website** (`hub/`, Next.js) searches packs and shows those two groups apart, with the
  registry's card. It shows no figure the registry did not compute, and no site report until
  sites sign what they share.

Running one, the administrator's routes and what to do when a pack must stop are in
[Running a pack registry](https://github.com/mendari-hq/mendari/blob/main/docs/operations/registry.md).
The docker sandbox runs a registry on port 8100 (`scripts/hub_seed.py` publishes the five packs).
Run `docker compose up --build` before `hub_seed.py`: the registry checks each pack against the
reference packs of its own image, so with an image built from an older checkout, a pack that
changed since is checked on RE2, and one whose patterns RE2 cannot run is refused.

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
verified emails to roles ([the Google setup](https://github.com/mendari-hq/mendari/blob/main/docs/deploying-with-phi.md#2-sign-in-and-roles)).

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
[ADR-0004](https://github.com/mendari-hq/mendari/blob/main/docs/decisions/ADR-0004-machine-access-tokens-and-scopes.md) and
[deploying with real patient data](https://github.com/mendari-hq/mendari/blob/main/docs/deploying-with-phi.md).

Every route has a permission, and anything unlisted needs admin:

| Role | May |
|---|---|
| viewer | read the dashboard and API |
| reviewer | also decide (Try it), resolve reviews, record outcomes, give feedback |
| service | an integration: read, decide, record outcomes |
| governance | read, approve automation |
| admin | everything else: evaluations, exports, chain verification; with a signed-in person, API keys, clients, members and invitations, and hidden evaluation sets (`curate`), but neither labels nor grades them |
| labeller | (memberships mode, a person) label the cases of hidden evaluation sets, and nothing else |

A hidden evaluation set is graded only with the organization's grading key, which only the
operator makes (`mendari access grader-key`): no person, administrator or other key may grade
(LP-11, [ADR-0011](https://github.com/mendari-hq/mendari/blob/main/docs/decisions/ADR-0011-hidden-evaluation-sets.md)).
The key holder keeps the sets from the command line (`mendari evalset create`, `add`,
`remove-case` and `list`; [the runbook](https://github.com/mendari-hq/mendari/blob/main/docs/operations/evalsets.md)):
hand-written cases only, sealed in the deployment's own database, with ids, keyed case hashes
and commitments in the chain (each case, label, drawn case's weight and stored set of aggregates,
and each frozen set's content), which `mendari audit verify --evalsets` checks the sealed rows
against, each case's status against its labels and draw. Their key rotates as the PHI key does (`mendari evalset rotate`, then
`rotate --check`), a new index key rebuilds the hidden-case index (`mendari evalset reindex`),
and `mendari evalset export` copies the sealed sets as stored, needing no key. Its cases are
labelled blind: `mendari evalset sample` draws them, seeded, within strata; `assign` gives each
to two of the organization's labellers (never an administrator), `adjudicate` a disputed one to
a third; `progress` counts them by status and labeller; and `agreement` and `coverage` measure
the set (rater agreement, and what its settled labels cover) and keep the result sealed. Once
every drawn case is settled, `mendari evalset freeze` fixes the set under its rotation rule
(`--max-runs` and `--max-age-days`, both required) and chains only a salted commitment to its
content; `show` reads its state, commitment, runs left and aggregates, and `retire` ends its use,
publishing nothing. A labeller labels through their own routes, mounted only in memberships mode
with both keys (`GET /api/labelling` for their counts, `GET /api/labelling/next` for their next
case, its input and the questions only, each read chained, and `POST /api/labelling/{id}` for
their label); an administrator never labels, and no key or token reaches them. The
organization's administrators follow and run the labelling through their own routes, mounted
likewise: `GET /api/evalsets` lists its sets, `GET /api/evalsets/{id}` reads one (its state,
commitment and runs left, its cases by status, each labeller's work by user id, and the
aggregates the key holder stored; never a case), and `POST /api/evalsets/{id}/sample`,
`/assignments` and `/adjudication` draw, assign and adjudicate as the commands do; another
organization's set is a `404` like an unknown one, and no route measures, freezes or retires.
In the dashboard, a labeller labels in **Labelling**, and the administrators run their sets in
**Hidden sets** (`#evalsets`, over those routes and the members list only): each set's state and
evaluation runs left, its cases by status and each labeller's work by name, drawing, assigning
and adjudicating, and the agreement (percent agreement as k of n with its interval, kappa and
alpha, "not estimable" in words) and coverage the key holder stored; never a case.

The organization's grading key grades a pack against one of its frozen sets through
`POST /api/evalsets/{id}/runs`, mounted with both keys in either account mode: with local
providers only, each run counted against the set's rotation rule and chained before its cases
are read, nothing stored, and answered with aggregates only (`mendari.hidden_result/1`, never a
case). The runbook walks
[a set from start to end](https://github.com/mendari-hq/mendari/blob/main/docs/operations/evalsets.md#a-set-from-start-to-end),
and `tests/test_evalset_end_to_end.py` runs that walk through the command line and the routes
on SQLite and Postgres. Hidden sets stay off on the hosted site until the owner makes and sets
the two keys ([the owner's steps](https://github.com/mendari-hq/mendari/blob/main/docs/operations/evalsets.md#the-owners-steps));
how labellers will reach it is the owner's decision still open.

With sign-in on, reviews, ratings, approvals and outcomes are recorded for whoever is signed in,
each new decision names who asked for it (`requested_by`, the requestor in the FHIR exports),
and the dashboard shows them as the audit chain names them and offers only what their role may
do. The chain names a person by their verified email, else by their identity provider's subject
qualified by its issuer (`<issuer>|<subject>`), never by a display name; a key as `key <id>`,
a Backend Services client as `client <id>`, and an EHR calling CDS Hooks with its own token as
`cds <id>` (in open mode too). In open mode the client names the reviewer,
reporter and approver, and each must be an identifier (no spaces, at most 100 characters). `MENDARI_OIDC_ORG_CLAIM` names the claim
that carries the person's organization: one deployment can serve several, and each sees only
its own decisions, reviews, feedback and reports.

**Memberships mode** (`MENDARI_ACCOUNTS=memberships`; the default is `claims`, all of the
above) takes each person's organization and roles from records instead of claims: an
organization is a record, and a person holds a membership of it with a set of roles, read on
every request ([ADR-0008](https://github.com/mendari-hq/mendari/blob/main/docs/decisions/ADR-0008-accounts-and-memberships.md)).
The role and organization claims are ignored. A person with no membership acts for no
organization, never `default`: they may ask to join one or accept an invitation made for their
verified address, and nothing else. The operator manages organizations with `mendari access`,
and each organization's administrators invite and manage their own people in the dashboard's
Access view (beside its keys and clients) or through the API; an invitation's link opens
`/invite`, where the person signs in and accepts it
([the runbook](https://github.com/mendari-hq/mendari/blob/main/docs/operations/accounts.md),
which also says how to switch a deployment). Once a deployment runs in memberships mode, every
chained event of an organization names it (the deployment's own events name none), and
`mendari audit verify` checks it against the row the event is about; each organization's administrators read and export their own administrative events
(`/api/audit/administrative`), and what is the whole deployment's (the chain's verification,
the deployment records, today's call totals) is the operator's, from the command line
(`mendari audit verify`, `mendari usage`, which also gives every organization's usage ledger:
remote calls and their cost by day). An organization's administrators see their own usage in the
Access view's Usage tab and at `GET /api/usage`. A test calls every route as two organizations and
fails on any answer that shows one the other's data (`tests/test_two_organizations.py`).

A link or form on another site cannot act with a signed-in browser, or sign it out: such a
request reaches only public routes and pages, with GET or HEAD (a `403` otherwise). If a proxy serves Mendari under a
name that is neither the request's host nor `MENDARI_PUBLIC_URL`, list it in
`MENDARI_TRUSTED_ORIGINS` (comma-separated origins).

**Governance approvals.** At risk level 2 or 3 a person reviews every decision (ADR-0002) until
the organization approves automation. Someone with the `governance` role gives the approval
from the Decision Card, with a reference such as committee minutes and an expiry. It covers one
exact pack version: a new digest needs a new approval. It is written into every decision it
affects, chained in the audit log, and can be revoked at once. Level 4 can't be approved.

**Monitoring.** The dashboard's Monitoring view follows the served version of a pack in use, for
the organization you act for, in whole weeks (14 to 180 days; other versions' decisions are
only counted). It shows observed accuracy (from reviews and outcomes recorded downstream), the
human override trend, confidence drift, the automated share, the decisions awaiting review and
the cost per decision (unknown when any decision's cost is), week by week. Refused inputs are
counted apart and left out of every rate and the cost. The benchmark is compared only when it
measured this build of the pack with the providers and models that run here, under the same
governance (an approval in force here and when it was measured, or neither), and only over
decisions made since that configuration started; otherwise the view says why, and only the
checks that need no benchmark run (automated decisions rated wrong, overrides that doubled,
operational health). Operational health comes from what each decision stored: the share with a
provider error recorded, the share that abstained, and how long decisions took (p50, p95,
mean).
Accuracy is compared stratum by stratum, as the benchmark measured it: reviewed decisions
against its reviewed accuracy, automated ones with an outcome recorded downstream against its
automated accuracy. Alerts fire only on enough evidence:

- a stratum's accuracy below the benchmark's by more than the pack's tolerance, beyond doubt
  (even the top of its 95% interval; 20 decisions or more);
- drift of 0.25 or more (PSI against the benchmark's confidences, over 50 or more);
- an automated decision someone rated wrong (a thumbs-down; ratings never change observed
  accuracy);
- an override rate that doubled against the week before (20 reviews or more in each);
- week over week, over 50 decisions or more this week: provider errors on 5% or more of
  decisions beyond doubt and doubled (on that level alone when the week before had fewer than
  50, as just after upgrading); with 50 or more the week before too, abstentions doubled and up
  5 points beyond doubt, and 5% or more of decisions beyond doubt taking twice the week before's
  p95 and 2 s or more.

For a scheduler (cron, a Kubernetes CronJob), `mendari monitor` checks the newest version of
each pack (or `--pack`), against its benchmark only for the build and providers a server
recorded running there (a model a setting chose is not recorded, so a benchmark of an LLM or
Jev model is compared by the server's view only) and the governance of `--org`. With
`--each-org` it checks each organization with decisions on its own, but the status checks'
(`MENDARI_STATUS_ORG`), and posts one alert per organization; a server does the same at
`GET /api/internal/monitor` with its `CRON_SECRET`. It exits 1 on any alert (or on an
error, such as a `--db` it cannot read: the line on stderr says which; with `--each-org`, 0
when every alert was posted, and 1 on an error or on alerts with nowhere to post them) and
posts them to `--webhook` / `MENDARI_ALERT_WEBHOOK` (https only; keep the URL as a secret,
since most carry their token in the path), as JSON or a Slack message
(`MENDARI_ALERT_FORMAT`), signed with `MENDARI_ALERT_SECRET`, tried up to three times (no
answer, `408`, `429` and `5xx` are tried again); it exits 3 when they could not be posted
([deploying with PHI](https://github.com/mendari-hq/mendari/blob/main/docs/deploying-with-phi.md), *Alerts*):

<!-- check: skip placeholder: posts alerts to a webhook you supply -->
```bash
mendari monitor --db "$DATABASE_URL" --each-org --webhook https://alerts.example.org/hook
```

After setting the webhook, `mendari alerts test` posts one test alert to
`MENDARI_ALERT_WEBHOOK` (`event` `alerts.test`, with no alerts in it), in its format and signed
with `MENDARI_ALERT_SECRET` as every alert is, tried once and recorded nowhere. It prints
whether it was delivered and the webhook's answer, never the URL or the secret, exits 1 when it
was not delivered, and refuses in CI (`CI` or `GITHUB_ACTIONS` set).

**PHI mode.** `MENDARI_PHI_MODE=hash` keeps no input at all, only its hash.
`MENDARI_PHI_MODE=encrypt` keeps inputs encrypted (AES-256-GCM, `MENDARI_PHI_KEY`) apart
from the audit chain. Only signed-in reviewers and admins read them, and each read is chained
(and logged).
Both modes need `MENDARI_INPUT_HASH_KEY` (32 random bytes, base64, not the PHI key): the input
hash becomes an HMAC (`hmac-sha256:<key id>:<hex>`) that guessing cannot match, and the engine
refuses to start without it. In both, the chained record keeps no evidence values (sealed with
the input under `encrypt`) and names a resource in an input issue by type and position
(`Patient (entry 2)`), not by its id; the caller's own answer is unchanged.
With sign-in on, the engine refuses to start under `off` (set or left unset), on SQLite and
Postgres alike: `off` is for a local sandbox without sign-in. Off Vercel, with only the EHR launch
on, it refuses to start until a mode is chosen (`off` written out starts, with a warning); on
Vercel the mode is `encrypt` unless set to `hash`, and `off` is refused. A database
once kept under `hash` or `encrypt` never takes plaintext again. `mendari phi purge` applies `MENDARI_RETENTION_DAYS` (default 30) without breaking the
chain; on Vercel a daily cron runs it once `CRON_SECRET` is set, and `mendari phi erase` erases
one decision's kept input on request. The PHI key rotates without losing anything
(`MENDARI_PHI_READ_KEYS`, then `mendari phi rotate`), the input hash key starts a new epoch, and
API keys rotate with an overlap. Notes (review
and rating notes, a payer's answer to a prior authorization) follow inputs: they are kept apart
from the chain, which holds only a salted commitment to each, sealed under `encrypt` and refused
under `hash`. Retention erases them too, `mendari notes erase` erases one at once, and
`mendari audit verify --notes` checks the kept ones. Hidden evaluation sets (LP-11)
have keys of their own, unlike every other key: `MENDARI_EVALSET_KEY` and
`MENDARI_EVALSET_INDEX_KEY`, set together for Production only, or not at all (the feature is then
off). Read
[`docs/deploying-with-phi.md`](https://github.com/mendari-hq/mendari/blob/main/docs/deploying-with-phi.md) before any real patient data.

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
([the formats](https://github.com/mendari-hq/mendari/blob/main/docs/spec/evaluation-reports.md)). What a site shares (`--share`) holds none of
it, nor subgroups: a closed format, `mendari.site-report/1`, of names and rounded numbers only
(`--share-out FILE` writes it to read first).

`mendari compare A B --dataset X` compares two runs (two configurations, or two versions of a
pack) case by case from their ledgers: each figure in each run with its interval, the cases where
they differ, an exact McNemar test, Holm-adjusted, and the cases that changed.

`mendari data coverage PACK --dataset X` says what a labelled set covers without running the
pack: its distinct cases, the distinct positives of each critical label, with a warning under
100, duplicates whose labels disagree, and the mix of languages, sources and strata. Duplicates
are cases whose inputs are equal once every string is NFKC-normalised and case folded,
invisible characters are removed and each run of other characters than letters, marks, digits
and `_` (punctuation, and symbols such as `<`, `>`, `+` and `%`) is one space (`norm/v1`), and
whole numbers are written as integers. It prints counts, never a case's text (`--ids` lists
duplicate ids):

<!-- check: run cli -->
```bash
uv run mendari data coverage mendari/ai-guard --dataset test
```

`mendari leaderboard` compares provider configurations on the same records and publishes
the result in `evals/leaderboard.json` and the Decision Card. Configurations whose credentials
exist only on a deployment (such as Jev through Vercel Connect) run through the deployment's
budgeted evaluation endpoint, and their reports are merged in with `--report`. An `llm` run asks
the model `LLM_MODEL` names (there is no default model):

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

The scoreboard and its scoped grades (LP-17, built but for two parts, below) follow a method
written down before their code: the
[grading method 1.0](https://github.com/mendari-hq/mendari/blob/main/docs/grading/method-1.0.md)
(`mendari.grading/1.0`). It sets the arms by provider class, which figures carry which interval,
the statistics (Clopper-Pearson simultaneous across a pack's critical labels, exact McNemar, the
paired bootstrap, Holm), the attestations a grade is made of (never a letter or a score), the
bands a ranking uses, the evidence rule (enough distinct positives for each critical label, never
fewer than 100) and the scope statement every result carries. Under it, grades and ranks are
given only on frozen hidden sets, and a public set is a regression check, never a ranking. The
leaderboard above is not made under it, and the pack's own sets it is measured on are public:
so `mendari leaderboard`, the Decision Cards and the dashboard show each of its tables as
"public set: regression check, not a ranking" with no rank printed, and link the scoreboard.
The rows are a listing order: those measured on this version of the pack, on records with
enough critical positives, first in `mendari.rank/1`'s order (the critical cases caught first),
then every other row by configuration name. A row's `rank` and `standing` stay in
`evals/leaderboard.json` and the API as data. The method is a draft,
not in effect, until the owner sets its effective date; `mendari.evals.method` pins its hash and
every number it sets ([ADR-0013](https://github.com/mendari-hq/mendari/blob/main/docs/decisions/ADR-0013-scoreboard-and-scoped-grades.md)).
It is an engineering evaluation, not clinical validation.

`mendari scoreboard arms PACK` lists a pack's provider configurations (one variant per slot) in
the scoreboard's four arms, by the models they ask through any slot: "Mendari only" (no
model), "+ specialised classifier" (Jev), "+ general LLM" and "+ both". It reads the manifest
alone, and says of each configuration whether it runs on the machine or what it calls off it
(CQL asks no model but calls Mendari's CQL engine). An arm a pack cannot build is
"not applicable" ([the format](https://github.com/mendari-hq/mendari/blob/main/docs/spec/evaluation-reports.md)).

A scoreboard entry (`mendari.scoreboard/1`, `mendari.evals.scoreboard`) is one configuration's
figures on one set, computed only from its ledger, its dataset, its run record (`mendari.run/1`:
each case's latency and tokens by model type, never a key, an endpoint or case text), a dated
price table and the method, so the same inputs give the same bytes. Evidence is decided on
distinct positives, the grade's bound is simultaneous across the critical labels, ECE is a
description, cost comes from tokens and the table (never a pack's or a provider's price), and on
a public set an entry is a regression check with no rank and no grade.

Results are kept in the repository, in an append-only store under `scoreboard/`: each result
named by its body's SHA-256, beside its ledger and its run record, each named by its own hash.
`mendari scoreboard run PACK [--dataset NAME] [--config slot=variant | --every-local]` runs a
configuration of a pack Mendari maintains over its public scoring sets (never `train`,
`calibration`, a file or a hidden set) and stores the result, unsigned; one still current is
kept, and a new one supersedes the old.
`mendari scoreboard add FILE` stores a result made elsewhere, never over another file.
`./scripts/build-scoreboard.sh` runs every local configuration of the five reference packs on
their 15 public scoring sets (18 results, about 2.7 MB) and writes the
[early scoreboard](https://github.com/mendari-hq/mendari/blob/main/docs/grading/scoreboard.md):
one table per pack and set, a regression check with no Rank column, each figure with its
interval, the insufficient-evidence banner (only the Referral Router's `test` set has enough
distinct positives on every critical label) and the marks "unsigned" and "method draft, not in
effect". CI's `reference-pack` job checks that nothing stored is removed or changed (but for a
valid signature added), that each result is what its inputs give, that each local ledger
re-runs to the same bytes and that a new remote result holds its replicates and its
declaration (`scripts/check_scoreboard.py`), and that the page is the store's
([Running the scoreboard](https://github.com/mendari-hq/mendari/blob/main/docs/operations/scoreboard.md)).
The same script writes the scores on the public page (`web/index.html`, between two marker
comments): the Patient Inbox Router's `test` set, every arm, each figure k of n with its
interval, and "not measured" for each arm with no result. It names no remote model, and writes
no configuration that calls one, until that provider type's disclosure is stated; the check
covers it too. Deploying `web/` stays the owner's.

A remote configuration (one that calls Jev, an LLM or Mendari's CQL engine) is run by hand,
never in CI: `mendari scoreboard run PACK --config slot=variant --replicates 3 [--prices FILE]`
refuses to start when `CI` or `GITHUB_ACTIONS` is set, and runs only the configuration the
pack's declaration (`scoreboard/declared/<pack name>.json`, committed before the run) names for
its arm. Replicate 1 is the headline, shown with the case flip rate between any two replicates
and the spread of each figure in its column. `mendari scoreboard canary PACK --config ...
--seed S` decides a seeded draw of 30 `test` cases once, by hand: a canary that drifts from the
replicates marks its arm's remote results stale, and another model reported, by a canary or a
later run of the same model type, marks every result of that type that named another. A result
with remote calls shows its figures only when each provider type it calls had a statement in
`scoreboard/disclosures.json` (none yet) when it was stored, and the file still holds it;
otherwise it reads "not published: disclosure pending", its figures kept off the page and `web/`.
The Clinical Trial Prescreener's CQL configuration is measured so, on Mendari's own CQL engine, and
stored pending; no Jev or LLM arm is measured.

A grade (`mendari.evals.grade`) is a set of attestations, each met, not met or insufficient
evidence, never a letter or a score: no critical case automated, each critical label's recall
floor, and the false-automation ceiling. "Insufficient evidence" means that even a perfect
result on those cases could not meet it. A ranking (`mendari.evals.bands`) orders the declared
arms graded in one read on a band tuple fixed by the method (the worst critical label's bound,
false automation, accuracy, Brier, then cost), lists an arm that fails a safety attestation or
lacks its evidence unranked, and gives each ranked arm a rank interval from a stated Holm family,
which never reorders the ranks. ECE and latency never enter it. Both are given only on frozen
hidden sets: on a public set the grade is null and the ranking is off.

The hidden-set scoreboard (`mendari.hidden_scoreboard/1`, LP-17k) grades them through the same
grading route as above: a run that sends `arms` (each declared arm with its configuration) and
`declared` (the SHA-256 of the committed declaration they come from) decides every declared arm
on the same cases in one grading read, one of the set's runs. Each declared configuration is
checked before any case is read: under the method 1.0 a hidden set is graded with local
providers only, so an arm that calls Jev or an LLM is refused, and the other arms read "not
graded here" (or "not applicable"). The answer holds, per graded arm, its figures with their
intervals, evidence on distinct positives, its attestations (draft while the method is not in
effect) and the insufficient-evidence banner; the ranking ("off: one arm graded" while only the
deterministic arm is local); the scope, with the set's id and freeze commitment; and the run's
number, outside its body's hash. Never cost, latency, a subgroup or a case. Without `arms`, the
route answers `mendari.hidden_result/1` as before. `mendari scoreboard add` stores the answer as
it was answered, and the owner signs it
([a hidden set's scoreboard](https://github.com/mendari-hq/mendari/blob/main/docs/operations/scoreboard.md#a-hidden-sets-scoreboard)).

Scoreboard results are signed by Mendari's owner with a grading key held offline
(`MENDARI_GRADING_SIGNING_KEY`), never by a server or by CI: `mendari scoreboard sign FILE` is
the key holder's, on their own machine, and refuses to run when `CI` or `GITHUB_ACTIONS` is set.
Anyone can check one with `mendari scoreboard verify FILE`, which needs no secret: it reads the
public keys in `scoreboard/keys.json` (none yet, so every result reads "unsigned"), refuses a
publisher's or a registry's key, and fails on any changed byte. The signature covers a prefix of
its own, so no checkpoint, archive or recall signature passes as a result's
([Verifying a scoreboard result](https://github.com/mendari-hq/mendari/blob/main/docs/grading/verifying.md)).

What is left is the owner's, once each and in order, in
[the owner's runbook](https://github.com/mendari-hq/mendari/blob/main/docs/operations/scoreboard.md#the-owners-runbook):
the method's numbers confirmed, the grading key made, the method published and its effective
date set, then the price table, the disclosures and the declarations written, the remote arms'
replicates run (LP-17i), the hidden sets graded, every new result signed, and the scoreboard
published. Results are signed only after the effective date, since setting it re-pins the
method's hash and a signature made before stops verifying. The developer flow, where a pack's
own author submits to a hidden set (LP-17l), waits for the pack store (LP-20).
`tests/test_scoreboard_end_to_end.py` takes a result from `mendari scoreboard run` to a signed,
verified result and a tampered one that fails, and a hidden set's answer from the grading route
to the store, with `mendari audit verify --evalsets` after it, on SQLite and Postgres.

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
yourself (`mendari/clinical-trial-prescreener:notes=jev`, `mendari/ai-guard:screen=rules_and_llm`;
an `llm` reader also needs `LLM_MODEL`, as there is no default model).

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
  each decision's reasons name what is missing. That Jev answers shows in `jev.calls_today` (in
  memberships mode, `mendari usage`) and in the decisions' errors.
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
| `LLM_MODEL` | Model for `llm` variants that pin none (a variant's `model` wins). There is no default: without either, the variant is `not_configured` and its decisions go to a person |
| `LLM_BASE_URL` | OpenAI-compatible endpoint instead of the AI Gateway, e.g. `http://localhost:11434/v1` |
| `AI_GATEWAY_API_KEY` | Gateway key for local use (on Vercel, the OIDC token is used) |
| `LLM_API_KEY` | Key for a non-gateway endpoint, if it needs one |
| `MENDARI_LLM_DAILY_LIMIT` | Daily call budget (default 500; `none` for no limit) |

As for Jev, these names are fixed, and an `llm` variant that names other variables
(`base_url_env`, `api_key_env`, `local_key_env`, `model_env`) is refused at load.

A calibration applies only to the model it was fitted on, so fit it on the model the
deployment asks: the variant's `model`, else the project's `LLM_MODEL` (which `vercel env run`
passes on). There is no default model; with neither, `mendari calibrate` refuses before it asks
anything.

<!-- check: skip secrets: needs your Vercel project and its credentials -->
```bash
vercel env pull .env.local   # a fresh OIDC token for local calls through the gateway
vercel env run -- uv run mendari calibrate <pack> --slot router --variant llm --limit 100
```

**Where provider calls may go.** `MENDARI_PROVIDER_ALLOWED_ORIGINS` (comma-separated origins,
like `MENDARI_FHIR_ALLOWED_SERVERS`) names the only origins Jev, LLM and Vercel Connect requests
may reach. Unset, nothing changes. Set, a request anywhere else is refused before it is sent
(`invalid_endpoint`: the question fails, so the decision goes to a person), an `LLM_BASE_URL` or
`JEV_API_URL` off the list stops the server at start-up, and signed-in `/api/health` shows the
list (`provider_allowed_origins`). With the AI Gateway (no `LLM_BASE_URL`), list
`https://ai-gateway.vercel.sh`; with `JEV_CONNECTOR`, list `https://api.vercel.com`. The CQL
engine stays on `MENDARI_FHIR_ALLOWED_SERVERS`.

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
   `CRON_SECRET` (16 random characters or more) for Production: the daily purge, monitor and
   checkpoint in `vercel.json` run only with it ([`docs/deploying-with-phi.md`](https://github.com/mendari-hq/mendari/blob/main/docs/deploying-with-phi.md), section 3).
3. Attach the Jev connector to the project and set `JEV_CONNECTOR`, `JEV_API_URL` and
   `JEV_MODEL`: the reference packs then run Mendari + Jev ([Jev](#jev)). The key is fetched at
   runtime with the deployment's OIDC identity; it is never stored in the project's environment
   variables. `MENDARI_PROVIDERS` changes any slot, for every pack or scoped to one
   (`mendari/ai-guard:screen=rules_and_llm`); a setting a pack cannot use is skipped and listed
   in `/api/health`. An unscoped setting, or a Jev default, never applies to a community pack:
   only a setting that names it does. An `llm` setting also needs `LLM_MODEL`
   ([LLMs](#llms-and-local-models)): there is no default model, and without it the `llm` member
   fails as `not_configured`, so a `rules_and_llm` slot runs its rules alone and none of its
   decisions is automated.
4. Turn on sign-in for Production ([Sign-in for organizations](#sign-in-for-organizations);
   the project uses Google, set for Production only). Until the launch, also set Vercel
   Authentication to protect **all deployments**: previews run without sign-in, and the default
   setting leaves the production domain public. After each deploy, check that an
   unauthenticated request to production gets redirected to login.
5. Push a branch to get a preview deployment, check it with
   `vercel curl /api/health --deployment <url>`, then merge to `main` for production.

**Limits and spending.** With sign-in on, the app's rate limits are on
(`MENDARI_RATE_LIMITS`, see [`docs/deploying-with-phi.md`](https://github.com/mendari-hq/mendari/blob/main/docs/deploying-with-phi.md)), and the
client address is Vercel's `X-Forwarded-For`. The edge layer is one Vercel firewall rule per IP
on `/auth/` and `/smart/`, kept as a file with its runbook in
[`deploy/vercel/`](https://github.com/mendari-hq/mendari/blob/main/deploy/vercel/README.md); it starts in log mode. The launch goes in this
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
`docker compose`. Status pages and uptime checks call `/api/health/ready` instead: public,
`GET` or `HEAD`, it reads the database (once every 5 seconds per instance at most, waited for
about 3.5 seconds) and answers `503` while the database does not answer or no pack is served,
where anonymous `/api/health` stays `ok` with sign-in on (without sign-in every caller gets the
settings, which read the database, so it fails during an outage too)
([deploying with PHI](https://github.com/mendari-hq/mendari/blob/main/docs/deploying-with-phi.md), *Health checks*).
`python scripts/status_checks.py --export json --api <url>` prints the status checks
(`deploy/status/checks.yaml`) ready to set up in a status provider, sending nothing: each
check's absolute URL, method, body, interval and what is up, with redirects not followed and
the decide key's variable named, never its value
([status checks](https://github.com/mendari-hq/mendari/blob/main/docs/operations/status-checks.md)).

One pack never stops the server. Its `invalid` lists the packs that failed to load (by path; an
error that is not Mendari's by its type alone), which are not served; the packs whose providers
could not be built (by id), which stay listed, counted in `packs` and offered in CDS Hooks
discovery, but whose decision and detail requests answer `422`; and the packs whose terminology
could not be built, which are served without it. Every other pack decides. `pack_warnings` lists,
by id, the served packs of your own whose provider configs, CDS Hooks prefetch or card summaries
`mendari validate` refuses (a key no provider reads, a variant that cannot run, a prefetch that
is not a read or search on the EHR's server, a placeholder that is not a bare name): they load
and serve what builds, and the start-up log says so too; a community or registry pack with such
a problem is refused, under `invalid`.

Days and times people see (the dashboard's times and daily volume, daily call counters, "today"
for criteria and ages, card dates) use `MENDARI_TIMEZONE`, an IANA name defaulting to
`America/New_York`. Stored timestamps, the audit hash chain, API JSON and FHIR stay UTC.

Preview and production deployments share the same Neon database unless Neon's preview branching
is enabled, so preview traffic appears in the production audit trail. With preview branching,
each preview gets a `preview/<git branch>` database branch. The Vercel-managed integration only
deletes it when Vercel deletes the deployment (months later), and a full branch limit makes
previews fail with "Resource provisioning failed". The `neon-preview-cleanup` workflow deletes
every preview branch whose Git branch has no open pull request and is gone or had one, when a
pull request closes and every six hours (or by hand, with a dry run); it needs a project-scoped
Neon API key as the `NEON_API_KEY` repository secret and the Neon project id as the
`NEON_PROJECT_ID` variable.

## Project layout

```text
src/mendari/
├── core/        spec, pack loading/digest, registry, runtime, policy, audit, hidden sets,
│                Decision Card
├── providers/   rules, naive_bayes (local ML), jev
├── standards/   fhir (R4 mapping, Task output), cds_hooks
├── evals/       metrics, harness, local-vs-benchmark comparison, pack tests, coverage,
│                a hidden set's grading result, the scoreboard's arms, entries and run records,
│                signed scoreboard results, grades and band ranking, the results store
├── server/      FastAPI app (REST, FHIR, CDS Hooks, review, audit, evals)
├── dashboard/   developer dashboard: HTML, CSS and ES modules, no dependencies or build step
└── sandbox/     synthetic data generator, FHIR seed/worker, demo
packs/           reference Decision Packs (maintained by Mendari)
community-packs/ community Decision Packs, contributed by pull request (MAINTAINERS.yaml)
datasets/        example "local site" dataset for validation
sandbox/inferno/ Inferno test kits against the engine (weekly CI job `conformance`)
scoreboard/      the results store: scoreboard results, their ledgers and run records, the packs'
                 declared configurations (declared/) and the public grading keys (keys.json)
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
uv run python scripts/check_scoreboard.py --base origin/main  # the results store: append-only
uv run python scripts/scoreboard_page.py --check   # the page and web/'s scores are the store's
```

## Roadmap

Full plan and status: [`docs/roadmap.md`](https://github.com/mendari-hq/mendari/blob/main/docs/roadmap.md). Done:
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

Apache-2.0. See [LICENSE](https://github.com/mendari-hq/mendari/blob/main/LICENSE), and
[NOTICE](https://github.com/mendari-hq/mendari/blob/main/NOTICE) for the third-party material and
terminology notices. Changes: [CHANGELOG.md](https://github.com/mendari-hq/mendari/blob/main/CHANGELOG.md).
