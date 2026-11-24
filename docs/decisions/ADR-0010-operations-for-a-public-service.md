# ADR-0010: Operations for a public service

## Status
Accepted (2026-11-07, the owner: "the patent gate is clear and I approve everything on my part",
which confirms each default below and 21n's merge). Proposed 2026-10-08. Built: 21a to 21n,
merged 2026-10-08 and 2026-10-09 (LP-21, `tasks/todo.md`); 21o, firewall enforcement, is the
owner's alone. The owner's actions stay open there: the status page provider, its checks and who
is paged; the alert webhook, its secret and format; the checkpoint key; and 21o. Each step added
what it built here, in the order they landed:
- 21a, the readiness route;
- 21c, the monitor comparing like with like;
- 21e, alert delivery;
- 21b, the status checks and page;
- 21d, operational health in the monitor;
- 21f, the monitor cron (`GET /api/internal/monitor` and `mendari monitor --each-org`, without
  the hosted schedule);
- 21l, the break-glass (`mendari access revoke` and `mendari access end-sessions`, each chained
  with who and why);
- 21j, audit checkpoints (`mendari audit checkpoint`, `mendari audit verify
  --since-checkpoint`; the templates run it daily);
- 21g, the usage ledger (`usage_ledger`, written with each decision and evaluation report);
- 21k, the signed daily checkpoint (`MENDARI_CHECKPOINT_KEY`, `GET /api/audit/checkpoint`,
  `mendari audit verify --against`; its hosted schedule came with 21n);
- 21h, evaluations' own quotas (`eval_jev`, `eval_llm` in `MENDARI_ORG_DAILY_LIMITS`, charged by
  `CallBudget.for_evaluation`, never the decisions' quotas);
- 21i, the usage screen (the Access view's Usage tab, `GET /api/usage` for administrators,
  `mendari usage` for the operator);
- 21m, the incident runbook (`docs/operations/incident-runbook.md`) and the restore test;
- 21n, the hosted schedule (`vercel.json` runs the purge, the monitor and the checkpoint daily),
  merged 2026-10-09 and run daily since; the owner's go-ahead, not recorded in writing then, was
  confirmed after the fact on 2026-11-07;
- after 21n, two aids for the owner's steps: the checks printed for a status provider
  (`scripts/status_checks.py --export json`) and one signed test alert (`mendari alerts test`,
  refused in CI).

The defaults below hold unless the owner chooses otherwise; the step that changes the hosted
firewall (21o) waits for the owner.

Terminology updated 2026-11-23: what this record called a pack is an agent.

## Context
The hosted platform is to take strangers without the owner watching it (LP-21, amending A13 of
the vision audit). What it had:

- **Health.** With sign-in on, anonymous `/api/health` answers three static fields and does no
  database work, so it stays green through a database outage. That is right for liveness, since a restart never fixes
  a database, but it means nothing outside the deployment can see an outage.
- **The monitor.** `mendari monitor` posts alerts to a webhook, and the self-hosted templates
  schedule it, but the hosted site does not. The monitor mixes organizations and agent versions,
  compares accuracy with a benchmark measured on other providers, and drops unknown costs from
  its averages. The webhook is unsigned and a failed delivery is only logged.
- **Usage.** Organization quotas cover decisions only. Evaluations spend the deployment's daily
  budget, so one organization's admin can exhaust it for everyone. No ledger says who spent
  which calls, tokens or dollars.
- **Incidents.** There is no incident runbook and no backup procedure. Revoking keys, clients
  and sessions in a hurry means the API or SQL.
- **The audit chain.** It is verified in full on every request, which gets slower as the chain
  grows. Nothing outside the database records what a past head was, so a truncated chain, or a
  restore from an old backup, still verifies.
- **The firewall.** Its rule is a file in log mode (`deploy/vercel`).

## Decision
LP-21 is built in small steps, each safe alone. None changes what a valid agent decides: no
agent, rule, policy or benchmark file is touched, and nothing sets an automation hold by itself.
Anonymous `/api/health` stays three fields with no database work.

1. **Readiness (21a).**
   - `GET`/`HEAD /api/health/ready` is public. It answers `200` with `ready` or `degraded`, or
     `503` `unready`, from:
     - one read of Mendari's own table, waited for about 3.5 s at most;
     - the agents as start-up found them: `degraded` while one did not load or build, `none`
       when no agent is served.
   - The answer holds fixed codes only: no error text, agent ids or counts.
   - Each instance reads the database at most once every 5 s, whoever asks, and keeps the
     answer (a failure too). The read runs in a thread of its own. One the database never
     answers (a network black hole, which no driver timeout bounds) holds that thread, and no
     second read starts until it returns. A caller that comes during a check gets the last
     answer at once, so readiness holds one request thread at most and never starves liveness.
   - That cache is its rate limit: the database work is at most one read per instance every
     5 s, whatever the request rate. There is no per-address limit (below).
   - Kubernetes' readinessProbe uses it, and liveness stays on `/api/health`.
2. **Status checks (21b).**
   - A checks file (`deploy/status/checks.yaml`, run by `scripts/status_checks.py` and
     described in `docs/operations/status-checks.md`) covers:
     - readiness;
     - the sign-in redirect, which must reach the identity provider (another host);
     - a synthetic decide on a bundled agent that spends no call of the deployment's shared
       Jev budget (a model-backed decide would spend one at every check), by a
       decide-only key in an organization of its own, read from `STATUS_DECIDE_KEY`, never the
       file; on a deployment with one organization (claims mode without an organization
       claim) the check waits until there is a second;
     - the registry's health;
     - the Hub's page.
   - A minimal public in-app status view, `/status`: static, reading readiness in the browser
     every 30 seconds, so serving it does no database work. The owner's external status page
     is the one that survives a full outage.
   - There is no dry-run decide mode: the status organization keeps its decisions apart. The
     setting that will leave it out of monitoring comes with the monitor's schedule (21f).
3. **The monitor (21c, 21d).**
   - It compares like with like (21c):
     - only the served agent version, other versions' decisions counted apart;
     - only decisions made since the current configuration (this build and these providers)
       started, by the deployment records;
     - the benchmark only when it measured this build (its digest), with these providers (each
       slot's variant, and its model where the server knows it; a command cannot see a model a
       setting chose), under the same governance (an approval in force or not). Otherwise no
       reference, with a reason, and only the checks that need none run (automated decisions
       rated wrong, overrides that doubled, operational health). A leaderboard row is not a
       fallback: its rows measure other datasets and versions;
     - accuracy stratum by stratum, as the benchmark measured it: reviewed decisions against
       one minus its expected override rate, automated ones with an outcome against its
       automated accuracy; the whole-set accuracy is shown, never compared;
     - an alert only beyond doubt: the top of the observed share's exact 95% interval below
       the reference minus the agent's tolerance, so an agent doing as its benchmark says alerts
       in under 1% of weeks;
     - refused decisions apart from every rate and the cost; drift over 50 confidences;
     - whole weekly buckets, 14 to 180 days;
     - an unknown cost shown as unknown.
   - Operational health comes from stored columns (21d), and needs no benchmark:
     - provider error share, from an additive `decisions.provider_errors` count (the questions
       with a provider error recorded, answered incompletely or not at all, written beside the
       record, never in it). Older rows are null and left out;
     - abstention share;
     - latency (p50, p95, mean of each decide).

     Each alerts week over week over 50 decisions or more this week: provider errors on 5% or
     more of decisions beyond doubt (the bottom of the interval) and doubled, or on that level
     alone when the week before had fewer than 50 counted; with 50 or more the week before too,
     abstentions doubled and up 5 points beyond doubt, and 5% or more of decisions beyond doubt
     taking twice the week before's p95 and 2 s or more (a p95 against a p95 flapped on a
     timeout tail; review of 21d). The thresholds are module constants (`evals/monitoring.py`),
     the same everywhere.
4. **Alerts (21e).** One module (`mendari.alerts`) renders, signs, sends, retries and records
   every alert, the monitor's and the quota alert alike.
   - `MENDARI_ALERT_FORMAT` chooses `mendari` (JSON, the default) or `slack`.
   - An optional `MENDARI_ALERT_SECRET` (at least 32 characters) signs every post:
     `Mendari-Signature: t=<unix>,v1=<HMAC-SHA256 of "<t>." + the body>`, with
     `Mendari-Delivery` and `Mendari-Event`. Without a secret the server warns; a bad format or
     secret, or a webhook address the client cannot parse, stops the server and the command.
   - No answer, `408`, `429` and `5xx` are tried again; any other answer is final. The monitor
     tries three times and exits 3 when its alerts could not be posted; the quota alert tries
     once on the decision's path (5 s), never failing the decision.
   - The server records every alert in `alert_deliveries` (its body, status, tries, last
     status, an error code; never the URL or the secret). One that may yet go through (no
     answer, `408`, `429`, `5xx`) stays `pending` for `retry_due`, five tries in all, which the
     scheduled monitor's route (21f) calls; any other answer is `failed` at once.
5. **The monitor cron (21f).**
   - `GET /api/internal/monitor` is guarded by `CRON_SECRET` before any database work, as the
     retention route is.
   - It first sends again the alerts still pending (`retry_due`, for 60 s at most), then
     monitors each organization with decisions in the last 30 days under its own tenancy,
     skipping the status organization (`MENDARI_STATUS_ORG`), and sends one recorded alert per
     organization that has alerts. The function stops at 300 s, so it starts no organization
     after 180 s, leaving the last one time for its posts; the ones left are counted and an
     alert names them, and each day starts at another organization, so none is left every day.
     A webhook it cannot use is logged and nothing is posted, never a failed start: the hosted
     site sets CRON_SECRET already.
   - `mendari monitor --each-org` does the same from the command line (read-only, recording
     nothing, so it never sends a pending alert again). It exits 0 when every alert was posted,
     1 on an error (an organization that fails does not stop the others) or on alerts with no
     webhook, 3 when one could not be posted. The templates run it daily; the Kubernetes job
     fails on 1 or 3 and is never re-run, which would post every alert again.
   - The hosted schedule (21n) changes the hosted site on deploy, so it merges only with the
     owner's go-ahead. Each job runs daily in an hour of its own (UTC): the purge at 08:00, the
     monitor at 09:00, the checkpoint at 10:00, two hours after the purge so the day's signed
     head covers what the purge chained. The alerts reach someone only once the owner sets
     the alert webhook (`MENDARI_ALERT_WEBHOOK`) for Production, one of the owner decisions
     for LP-21 in `tasks/todo.md`; until then gate `hosted-monitoring` stays pending.
6. **Usage (21g to 21i).**
   - A `usage_ledger` per day, organization, provider type and purpose counts calls, priced
     calls, tokens and cost.
   - A cost is shown as unknown unless every call was priced.
   - Rows are written in the decision's own transaction; providers never write to the database.
   - Evaluations count against the organization that runs them through their own quotas
     (`eval_jev`, `eval_llm`; 21h), unset meaning no limit, never the decisions' quotas, so no
     live decision is lowered through its organization's quotas by an evaluation. Evaluations
     still spend the deployment's daily budgets, which live decisions share: the evaluation
     quotas are to be set below them (a startup warning when one is not). Evaluations get no
     deployment budget of their own (the owner, 2026-11-07).
   - Admins see their own organization's usage on a usage screen. The operator sees the whole
     deployment from the command line.
7. **Checkpoints (21j, 21k).**
   - Verification runs incrementally from a stored checkpoint (`audit_checkpoints`, outside the
     chain).
   - A daily statement `{format, date, head, key_id, signature}` is signed with Ed25519
     (`MENDARI_CHECKPOINT_KEY`) and published once the owner sets the key. It carries no
     sequence number and no count.
8. **Incidents (21l, 21m).**
   - Break-glass commands revoke keys and clients and end sessions, each chained.
   - `docs/operations/incident-runbook.md` covers:
     - severities tied to triggers;
     - paging;
     - holding automation;
     - revoking access;
     - notifying users;
     - backup and restore, followed by a full chain verification against the last signed
       checkpoint.
   - A restore test runs in CI on SQLite and Postgres.
9. **Firewall (21o).** It is enforced only on the owner's confirmation, after the log-only week,
   whose results are recorded in the runbook. The build never changes the hosted firewall.

## Consequences
- **Readiness.**
  - An outage is visible to status pages and probes, while liveness still never restarts a
    server over its database.
  - A check that misses the cache touches the database. A check more frequent than the
    database's idle suspend keeps the database awake.
  - On Vercel, an instance that starts while the database is down fails at import, before any
    route.
  - Readiness has no per-address limit: a flood of it is the edge's (Vercel's firewall, a
    site's proxy), as for `/api/health`. The cache bounds the database work it can cause, and
    the bounded check the threads it can hold.
- **Monitor alerts** stop firing on comparisons that were never comparable, and start firing on
  provider errors, abstentions and latency.
- **Shared files.** Several steps touch the same files (`server/app.py`, the route inventories,
  `core/audit.py`'s tables), so each one rebases on the one merged before it.

## Alternatives considered
- **Deep checks in `/api/health`.** Rejected: it is the liveness probe, and LP-21 forbids
  database work in its anonymous answer.
- **Counting readiness in the database limiter.** Rejected: an outage would turn every readiness
  request into the limiter's error instead of a `503`.
- **A per-address readiness limit, counted in memory.** Built first, then removed in review: a
  `429` fails a probe, and a probe can share its address with other callers (a kubelet behind
  SNAT, a load balancer's health checker, or any pod naming the kubelet's address in
  `X-Forwarded-For` inside a trusted proxy range), who could then take a pod out of service.
- **Waiting for a check in progress.** Rejected: a read the database never answers kept every
  later caller waiting in a request thread, until liveness had none left (found in review).
- **A bare `SELECT 1`.** Rejected: it passes on an empty file or another database. The check
  reads the chain's table.
- **Charging evaluations to the live quotas.** Rejected: an evaluation could then push an
  organization's live decisions to review, which is clinical behaviour.
