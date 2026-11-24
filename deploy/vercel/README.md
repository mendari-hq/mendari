# Vercel firewall rules for Mendari

Mendari limits requests in two layers ([ADR-0005](../../docs/decisions/ADR-0005-hardening-before-phi.md)):

- **The edge (this file):** Vercel's firewall limits per IP address, and only on the routes
  anyone can reach without signing in: `/auth/` (sign-in, its callback, logout, the token
  endpoint) and `/smart/` (the EHR launch). It stops a flood before it reaches the function, at
  no cost for the requests it refuses. There is no per-IP rule on `/api`, `/fhir` or
  `/cds-services`: a hospital sends every clinician's and every EHR's traffic from one address.
- **The app:** Mendari itself limits per API key, client or person on the costly routes
  (decisions, guard checks, evaluations, exports), and per address on sign-in and the token
  endpoint, in the database so every instance shares the count (`MENDARI_RATE_LIMITS`; see
  [`docs/deploying-with-phi.md`](../../docs/deploying-with-phi.md)). Its `429` carries
  `Retry-After` and the route's own error shape. The firewall's `429` is Vercel's own page: rely
  on the app's answers in clients.

[`firewall-rules.json`](firewall-rules.json) holds the one rule, 120 requests a minute per
address on `/auth/` and `/smart/`. The app's own sign-in limit per address is never below it
(300 a minute, bursts of 120), since everyone behind one address shares it; the app limits the
token endpoint to 60 a minute per address itself. So, once enforced, the firewall is the
tighter layer on sign-in, and stops a flood before it costs a function call. One rule fits
every plan (Hobby allows one rate-limit rule). It has no persistent action: an address over
the limit is never blocked for longer than the window, so the owner behind a shared network is
never locked out for long.

## Apply it

The rule is not in `vercel.json` (its `mitigate` takes only `deny` and `challenge`). Add it with
the Vercel CLI, logged in to the project's team, then publish:

```bash
vercel firewall rules add --json "$(jq -c '.rules[0]' deploy/vercel/firewall-rules.json)" --yes
vercel firewall publish
vercel firewall rules list --expand   # check it is there, in log mode
```

Changes apply at once, without a redeploy.

## Log mode first, for 7 days

The rule starts with `rateLimit.action: log`: it counts and records, and refuses nothing. For
the first week, look in the project's Firewall tab for addresses it would have limited. A
legitimate address there (a hospital's network, the owner's VPN) means the limit is too low:
raise `limit` before enforcing.

Then enforce it: in the dashboard (Firewall, the rule, the action when the limit is exceeded:
"Too Many Requests"), or set `rateLimit.action` to `rate_limit` in the JSON, remove the old rule
(`vercel firewall rules remove "Mendari sign-in per IP" --yes`), add it again as above and
publish.

## If it gets in the way

`vercel firewall rules disable "Mendari sign-in per IP"`, then `vercel firewall publish`:
immediate, no redeploy. The app's own limits stay; to turn those off in effect, set
`MENDARI_RATE_LIMITS=log` for Production, redeploy, and confirm `rate_limits.mode` in
`/api/health` signed in.

## An EHR's own CDS Hooks tokens

Off on the hosted project until a pilot is signed: then `MENDARI_CDS_CLIENTS` (and any
`MENDARI_SITE_CODES`) is set for Production only, with the EHR's JWK Set origin and FHIR base on
`MENDARI_FHIR_ALLOWED_SERVERS`. The open-mode sandbox flag is never set here: the server refuses
to start with it on Vercel. The firewall stays off `/cds-services`, so these calls meet the app's
limits only. One EHR's calls all come as one principal, `cds:<id>`, under the app's per-principal
decide limit (600 a minute, bursts of 100) and `MENDARI_PRINCIPAL_DAILY_LIMIT`: measure the
pilot's volume before it goes live. Each function instance keeps its own copy of the EHR's keys,
so the EHR sees up to one request a minute for them from each instance, and a key it retires
still verifies for up to an hour on an instance that kept it. Taking a client out of
`MENDARI_CDS_CLIENTS` stops it at the redeploy
([Connecting an EHR](../../docs/guides/connecting-an-ehr.md)).

## Registry and Hub

The agent registry runs inside the engine's project, on its database (LP-19's option A,
[ADR-0015](../../docs/decisions/ADR-0015-registry-accounts-under-the-engine.md)): with
`MENDARI_REGISTRY=on` it answers at `/registry/v1/...` on the engine's own origin. It needs no
project, database or function setting of its own: `vercel.json` already gives every request
300 s, and a publish's regression check stops at 180 s (`tests/test_vercel.py` pins both). The
Hub, the public site that reads the registry, is a second Vercel project built from `hub/`
(`hub/vercel.json`). Nothing here is on until the owner sets it; no release sets it.

Turn it on in this order, each step checked before the next:

1. **The review organization's record**, made on the engine's database:
   `mendari access org create <review org> --name "Registry review" --db "$DATABASE_URL"`.
2. **The registry's key**, for Production only, as a Secret: `MENDARI_REGISTRY_SIGNING_KEY`, 32
   random bytes in base64 (`openssl rand -base64 32`). Keep a copy where the project's
   credentials cannot reach it. Without it the registry recalls nothing itself and counter-signs
   nothing.
3. **The switch**: `MENDARI_REGISTRY=on` and `MENDARI_REGISTRY_REVIEW_ORG=<review org>`, then
   redeploy. Check: `GET <engine>/registry/v1/health` answers `200`;
   `GET <engine>/registry/v1/registry-key` answers `200` with the key's id; signed in,
   `/api/health` says `registry: true`; the server's log says nothing about reviewing being off.
4. **A reviewer**: an administrator of the review organization gives a member the role
   `registry_reviewer` (the Access view). The Registry review screen then shows them the queue.
5. **The Hub's project**: a new Vercel project from this repository with the root directory
   `hub` (its `vercel.json` sets the framework and the build). Set `REGISTRY_URL` to the
   engine's public origin followed by `/registry`, with no trailing slash, then add the Hub's
   domain. A commit that changes nothing under `hub/` builds nothing there (the ignored-build
   step): the Hub reads the registry at request time, so a registry change needs no Hub build.
6. **The status checks**: the registry's base is the engine's origin followed by `/registry`
   (`--registry <engine>/registry`), and the Hub's is its domain
   ([status checks](../../docs/operations/status-checks.md)).
7. **The chain's head kept elsewhere**: `mendari registry checkpoint` once a day, from a
   machine that reads the database and holds the key, its statements kept where no credential
   of the project can write ([the chain](../../docs/operations/registry.md#the-chain)).

The firewall stays off `/registry` for the reason it stays off `/api`: publishers publish from
CI, behind shared addresses. The registry limits in the app: reads per address, writes per
namespace (`MENDARI_REGISTRY_RATE_LIMITS`), counted in the engine's database.

What to watch, in the project's Observability tab and the database's console:

- **Function duration on a publish** (`PUT /registry/v1/agents/...`). Near 180 s, the regression
  check is running out of time: it then records that it failed and the publish still succeeds
  (LP-18g). If the check cannot start on the platform at all, every publish says so
  (`failed: the check could not start`); `MENDARI_REGISTRY_REGRESSION=off` then publishes
  without the check, instead of recording a failed one on every version.
- **Database size.** Archives are kept in the database (`hub_archives`), each at most the
  engine's request body limit (`MENDARI_MAX_BODY_BYTES`, under Vercel's 4.5 MB).

To turn it off, unset `MENDARI_REGISTRY` and redeploy: `/registry/` and `/api/registry/` answer
as they did before, nothing is deleted (every `hub_` row stays, for when it is turned on again),
and the Hub shows that it cannot reach the registry. Unsetting `MENDARI_REGISTRY_REVIEW_ORG`
alone leaves the registry up and takes reviewing back to the operator's commands
(`mendari registry approve|refuse`).

## Order at launch

All of Phase 4b, the external review and its fixes, this rule enforced after its week in log
mode, then Vercel Authentication lifted.
