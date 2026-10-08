# Status checks

Two things tell people whether Mendari is working:

- **The deployment's own status page**, `/status`. It is public and static, and reads this
  deployment's readiness (`/api/health/ready`) in the browser every 30 seconds: whether the
  database answers and the packs loaded. It is served by the deployment it reports on, so an
  outage that stops the deployment answering at all takes the page down with it.
- **An external status page**: a status provider that checks the deployment from outside,
  several times an hour from more than one region, and keeps a public page and a history that
  survive any outage of Mendari's. This is the one people should be pointed to. Choosing the
  provider, and who it pages, is the owner's (gate `status-page` in `launch/gates.yaml`).

## The checks

[`deploy/status/checks.yaml`](https://github.com/Junebugg1214/mendari/blob/main/deploy/status/checks.yaml)
lists them; set each up in the provider as an HTTP check:

| Check | Request | Up when | Why |
|---|---|---|---|
| Readiness | `GET /api/health/ready` | `200` | The database answers and the packs loaded. `503` when not. |
| Sign-in | `GET /auth/login?next=%2F`, redirects not followed | `302` to the identity provider (another host) | Sign-in works. Only with sign-in on. |
| Decide | `POST /api/packs/mendari/patient-inbox-router/decide` with the status key and the file's message | `200` with an `outcome` | A decision is made and recorded, end to end. |
| Registry | `GET <registry>/v1/health` | `200` | Packs can be published and pulled. |
| Hub | `GET <hub>/` | `200` | The Hub's site loads. |

Never use `/api/health` for any of them. With sign-in on, it answers `ok` without touching the
database, so it stays green through a database outage. It is for liveness probes, which should
not restart a server over its database.

A readiness answer of `200` with `status: degraded` means a pack did not load or build: the
deployment works, and signed-in `/api/health` lists the pack under `invalid`. Treat it as up.
To be told about it as well, add a keyword check for `"status":"ready"`.

Five minutes apart is enough for each check. Each instance reads the database at most once
every 5 seconds whatever asks, so checks add little load, but any check that reaches the
database keeps it awake: on Neon, a check more often than its idle suspend (5 minutes by default)
means the compute never sleeps.

Each sign-in check spends one of its address's sign-in requests (300 a minute, 120 at once, in
Mendari; 120 a minute per address on `/auth/` at Vercel's firewall), and the decide check one of
its key's decisions. At five minutes apart neither comes close.

## The status key

The decide check needs a key of its own, and nothing more. Its decisions are real: they are
recorded and chained. So give the key:

- **its own organization**, so its decisions are never mixed with anyone's in the review queue,
  monitoring, usage or quotas. Something like `status`, and nothing else in it;
- **the decide scope only**: `role` `service` with `scope`
  `https://mendari.co/fhir/scope/decide`. It can then decide and list the packs, and nothing
  else.

The decide check sends a fixed synthetic message to the inbox router, which runs local providers
by default, so the check spends no model calls. If your deployment runs the inbox on Jev or an
LLM, each check spends one call of the daily budget.

In memberships mode ([Organizations and memberships](accounts.md)):

1. Create the organization: `mendari access org create status --name "Status checks"`.
2. Invite yourself as its administrator:
   `mendari access invite --org status --email <your address> --role admin`.
3. Accept the invitation, choose the `status` organization, and create the key in the Access
   view, or with `POST /api/auth/keys`:
   `{"name": "status checks", "role": "service", "scope": "https://mendari.co/fhir/scope/decide"}`.

In claims mode, the organization is the one the identity provider's claim names. An
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
