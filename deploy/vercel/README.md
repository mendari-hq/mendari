# Vercel firewall rules for Mendari

Mendari limits requests in two layers ([ADR-0005](../../docs/decisions/ADR-0005-hardening-before-phi.md)):

- **The edge (this file):** Vercel's firewall limits per IP address, and only on the routes
  anyone can reach without signing in: `/auth/` (sign-in, its callback, logout, the token
  endpoint) and `/smart/` (the EHR launch). It stops a flood before it reaches the function, at
  no cost for the requests it refuses. There is no per-IP rule on `/api`, `/fhir` or
  `/cds-services`: a hospital sends every clinician's and every EHR's traffic from one address.
- **The app:** Mendari itself limits per API key, client or person on the costly routes
  (decisions, the AI guard, evaluations, exports), and per address on sign-in and the token
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

## Order at launch

All of Phase 4b, the external review and its fixes, this rule enforced after its week in log
mode, then Vercel Authentication lifted.
