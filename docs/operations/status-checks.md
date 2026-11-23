# Status checks

Two things tell people whether Mendari is working:

- **The deployment's own status page**, `/status`. It is public and static, and reads this
  deployment's readiness (`/api/health/ready`) in the browser every 30 seconds: whether the
  database answers and the agents loaded. It is served by the deployment it reports on, so an
  outage that stops the deployment answering at all takes the page down with it.
- **An external status page**: a status provider that checks the deployment from outside,
  several times an hour from more than one region, and keeps a public page and a history that
  survive any outage of Mendari's. This is the one people should be pointed to. Choosing the
  provider, and who it pages, is the owner's.

## The checks

[`deploy/status/checks.yaml`](https://github.com/mendari-hq/mendari/blob/main/deploy/status/checks.yaml)
lists them; set each up in the provider as an HTTP check ([Exporting the
checks](#exporting-the-checks) prints them ready to copy):

| Check | Request | Up when | Why |
|---|---|---|---|
| Readiness | `GET /api/health/ready` | `200` | The database answers and the agents loaded. `503` when not. |
| Sign-in | `GET /auth/login?next=%2F`, redirects not followed | `302` to the identity provider (another host) | Sign-in works. Only with sign-in on. |
| Decide | `POST /api/packs/mendari/portal-message-agent/decide` with the status key and the file's synthetic message | `200` with an `outcome` | A decision is made and recorded, end to end. |
| Registry | `GET <registry>/v1/health` | `200` | Agents can be published and pulled. A registry under the engine (`MENDARI_REGISTRY=on`) has the base `<engine>/registry`, so the check is `GET <engine>/registry/v1/health`. |
| Hub | `GET <hub>/` | `200` | The Hub's site loads. |

Never use `/api/health` for any of them. With sign-in on, it answers `ok` without touching the
database, so it stays green through a database outage. It is for liveness probes, which should
not restart a server over its database.

A readiness answer of `200` with `status: degraded` means an agent did not load or build: the
deployment works, and signed-in `/api/health` lists the agent under `invalid`. Treat it as up.
To be told about it as well, add a keyword check for `"status":"ready"`.

Five minutes apart is enough for each check. Each instance reads the database at most once
every 5 seconds whatever asks, so checks add little load, but any check that reaches the
database keeps it awake: on Neon, a check more often than its idle suspend (5 minutes by default)
means the compute never sleeps.

Each sign-in check spends one of its address's sign-in requests (300 a minute, 120 at once, in
Mendari; 120 a minute per address on `/auth/` at Vercel's firewall), and the decide check one of
its key's decisions. At five minutes apart neither comes close. Point the sign-in check at the
deployment's public address (`MENDARI_PUBLIC_URL`): on any other, Mendari first moves the
browser there, and that move is not the identity provider answering (the script says so).

## The status key

The decide check needs a key of its own, and nothing more. Its decisions are real: they are
recorded and chained, and each waits in its organization's review queue (the demo agent sends a
message that asks for no refill or appointment to the front desk). So give the key:

- **its own organization**, so its decisions never reach anyone else's review queue, quotas or
  monitoring view. Something like `status`, and nothing else in it. Two things still count it
  with everyone: `mendari monitor` without `--org` (all organizations together; the scheduled
  monitor and `--each-org` leave it out once `MENDARI_STATUS_ORG` names it), and the
  deployment's daily call budgets;
- **the decide scope only**: `role` `service` with `scope`
  `https://mendari.co/fhir/scope/decide`. It can then decide and list the agents, and nothing
  else.

The check sends a fixed synthetic message to the bundled demo agent. Offline (no
`MENDARI_AGENT_URL`) it answers from its canned responses and calls nothing, and with sign-in
on none of those decisions automates (each goes to a person, reason `canned answers:
MENDARI_AGENT_URL is not set; a person decides`). With
`MENDARI_AGENT_URL` set, every check calls that agent: 288 calls a day from one region at five
minutes apart, so set the agent's own rate limits with that in mind, or choose its `rules`
variant for the status organization's deployment (`MENDARI_PROVIDERS=reader=rules`).

**One organization.** In claims mode without an organization claim (`MENDARI_OIDC_ORG_CLAIM`
unset, as with Google sign-in), every person and key belongs to `default`: there is no second
organization to give the key. Leave `STATUS_DECIDE_KEY` unset there (the check is skipped)
until the deployment has an organization claim or runs in memberships mode.

**Expiry.** A key expires after 90 days unless you ask for longer (`"days"`, at most 365), and
the decide check fails from then on. Ask for 365 and replace it before then:
`POST /api/auth/keys/{key_id}/rotate` (with `"days": 365`) gives a new key, and the old one
keeps working for 24 hours, time to give the provider the new one.

In memberships mode ([Organizations and memberships](accounts.md)):

1. Create the organization: `mendari access org create status --name "Status checks"`.
2. Invite yourself as its administrator:
   `mendari access invite --org status --email <your address> --role admin`.
3. Accept the invitation, choose the `status` organization, and create the key in the Access
   view, or with `POST /api/auth/keys`:
   `{"name": "status checks", "role": "service", "scope": "https://mendari.co/fhir/scope/decide", "days": 365}`.

In claims mode with an organization claim, the organization is the one the claim names. An
administrator whose claim names the status organization creates the key the same way.

Give the key to the status provider as a secret header, `Authorization: Bearer <key>`, never in
the check's URL. If it leaks, revoke it in the Access view and make another.

## Trying the checks

`scripts/status_checks.py` runs the same file from any machine, which is a quick way to see what
the provider will see before setting it up:

<!-- check: skip placeholder: runs against your deployment's URLs and key -->
```bash
STATUS_DECIDE_KEY=<the status key> python scripts/status_checks.py \
  --api https://mendari.example.org --registry https://registry.example.org \
  --hub https://hub.example.org
```

It prints one line for each check (`ok`, `failed` and why, or `skipped` and why) and exits 1
when any check fails. A check is skipped when its base URL is not given, when the key is not
set, or (sign-in) when sign-in is off. Nothing it prints holds the key.

## Exporting the checks

To set the checks up in a provider, print them from the same file:

<!-- check: skip placeholder: prints the checks for your deployment's URLs -->
```bash
python scripts/status_checks.py --export json \
  --api https://mendari.example.org --registry https://registry.example.org \
  --hub https://hub.example.org
```

It sends nothing. It prints, as JSON, each check whose base URL you give, with what the
provider needs to run it as the script does:

- `method`, `url` (absolute), `json_body` when there is one, and `interval_seconds`;
- `follow_redirects`, always `false`. Each check judges the answer it gets: a redirect followed
  to a sign-in page would answer `200` and look up;
- `expected_statuses`, the statuses that are up;
- `location_leaves_host` and `location_path_not` (sign-in): up only when the redirect's
  `Location` names another host than this one, and its path is not `/auth/login`. That path on
  another host is Mendari moving sign-in to its public address first, not the identity
  provider: export with `--api` set to that address (`MENDARI_PUBLIC_URL`);
- `body_keyword` (decide): text the body must hold, `"outcome":`;
- `headers`: `Authorization: Bearer ${STATUS_DECIDE_KEY}` names the variable that holds the
  key (`secret_from_environment`), never the key. Give the provider the key itself as a
  secret;
- `only_with_sign_in` (sign-in): set it up only where sign-in is on.

A check whose base URL is not given is left out, with a line on stderr saying so. A key in the
checks file the export does not know stops it, so no part of a check is dropped unsaid. Your
provider's own import format, API or Terraform is not written yet: copy each check into it.

## The project's own deployment

The project's own deployment (synthetic data, on Vercel) no longer sits behind Vercel
Authentication: since it was lifted from Production (see the launch order in
[Before go-live](../deploying-with-phi.md#9-before-go-live)), only Preview deployments are
protected, and an outside check reaches Production's public routes. Never give a provider the
Protection Bypass for Automation secret to reach a Preview: that secret passes every
protection Vercel puts on the deployment, not one check. On that deployment the decide check also waits
for a status organization (it runs in memberships mode since 2026-11-07, and has no status
organization yet: it is made as above), and the
registry and Hub checks wait until both are hosted (the registry under the engine, its base the
engine's origin with `/registry`; the Hub on a project of its own:
[Registry and Hub](https://github.com/mendari-hq/mendari/blob/main/deploy/vercel/README.md#registry-and-hub)).

