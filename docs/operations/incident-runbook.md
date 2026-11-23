# Incident runbook

What to do when something goes wrong with a Mendari deployment. This page covers how bad it is,
who is told, the first moves (hold automation, revoke access), telling the people affected, and
getting the database back from a backup with its audit chain checked.

Each step names the command or page that does it. The commands write to the server's database
directly (`--db`, or `$DATABASE_URL`), so they work with the server down.

Keep a copy of this page where the people on call can read it when Mendari cannot be reached.

## How bad is it

| Severity | When | First response |
|---|---|---|
| **SEV1** | Possible harm to a patient, or a breach. Any of: an emergency message automated or routed away from a nurse; one organization's data shown to another; a key, client or session misused to read cases; the audit chain fails verification (`mendari audit verify`, or an `audit_broken` alert); every decision failing for 15 minutes or more. | At once, day or night: hold automation for what is affected (everything, if unsure), revoke what was misused, page the owner. |
| **SEV2** | Safety margin lost, no known harm. Any of: a monitor alert `automated_wrong` or `accuracy`; `provider_errors`; readiness failing (`503` from `/api/health/ready`) for less than 15 minutes; a leaked key that may only decide; alerts that could not be posted: `mendari monitor` exit 3, a scheduled monitor answering `not_delivered` or `retried.failed` above 0, or deliveries left `pending` or `failed` (`failed` after one try usually means the webhook's address or token is wrong). | Within the working day: hold the agent if its decisions are automated, then find the cause. |
| **SEV3** | Service worse, not less safe. Any of: `drift`, `overrides`, `latency` or `abstention` alerts; a quota reached (`quota_reached`); a run that could not reach every organization (`monitor_incomplete`). | Next working day. |

Automation only ever falls back to people. A hold, a quota, a failed provider or a passed
deadline sends decisions to the review queue: a degraded Mendari is slower, never more automatic.
Treat anything that looks like the opposite as SEV1.

## Who is paged

- **The owner**, the deployment's operator, through the status provider's paging and the alert
  webhook (`MENDARI_ALERT_WEBHOOK`). Until there is a rota the owner is on call. Who else is
  paged, and how, is the owner's to set ([status checks](status-checks.md), gate `status-page`).
- **An organization's administrators**, for what is theirs: they see their organization's
  administrative events (keys, clients, holds, members) in the Access view and at
  `/api/audit/administrative`.
- Alerts carry ids and counts, never case text, so a page never holds a patient's data.

## First moves

1. **Hold automation** for what is affected, so every decision goes to a person while you find
   out ([Holding automation](automation-hold.md)). From the command line, for every
   organization:

   ```bash
   mendari automation hold --all-orgs --reason "incident 7" --db "$DATABASE_URL"
   ```

2. **Revoke access** that was misused or may have been: a key, a client, every credential of an
   organization, everyone's sessions ([Revoking access](revoking-access.md)):

   ```bash
   mendari access revoke key <id> --reason "incident 7" --db "$DATABASE_URL"
   mendari access end-sessions --kind all --reason "incident 7" --db "$DATABASE_URL"
   ```

   `--kind all` signs out every person and every EHR launch; `--kind people` leaves the
   launches signed in, and `--kind launches` the people.

3. **Keep the evidence** before changing anything else:
   - the chain's state (`mendari audit verify --db "$DATABASE_URL"`, and note its `head`);
   - what the incident touched (`mendari audit export --db "$DATABASE_URL" --out ...`, or
     `--administrative` for keys, clients and holds);
   - the security log lines for the window (stdout, JSON lines).

## Telling people

- **The organizations affected**: tell their administrators, whom the organization ids in the
  alerts and the administrative events identify. Say what happened, when, what was held or
  revoked, and what they must do (make a new key, sign in again). Never put case data in a notice.
- **Everyone using the deployment**: post an outage on the status page (the owner's provider).
  Use the same words, and leave out anything about any one organization.
- **What the law asks**: a deployment that holds patient data follows its own agreements, and
  the law that applies to it, on telling people. This runbook does not replace them. The
  project's own public deployment holds synthetic data only.

## Backup and restore

Back up the whole of the database the server writes (`DATABASE_URL`). The chain, the decisions,
the holds, the keys and the memberships are all in it.

| Database | Back up | Restore |
|---|---|---|
| SQLite | `sqlite3 var/mendari.db ".backup 'mendari-backup.db'"` (consistent while the server runs) | stop the server, then move `var/mendari.db`, `var/mendari.db-wal` and `var/mendari.db-shm` aside together and keep them (they are the incident's database: a crashed server leaves the last two, which SQLite would otherwise replay onto the backup), then copy the backup to `var/mendari.db` |
| Postgres | `pg_dump --format=custom --file mendari.dump "$DATABASE_URL"` | into an empty database: `pg_restore --no-owner --dbname "$RESTORE_URL" mendari.dump` |
| Neon (the hosted deployment) | Neon keeps the database's history | create a **new branch** from a point in time and use its direct (unpooled) address as `$RESTORE_URL` for the steps below; only then point the deployment's `DATABASE_URL` at it and redeploy. Never restore the branch `DATABASE_URL` serves in place while the deployment runs: it would serve the restored rows (keys revoked since, holds set since: gone) before anything below is done. If you must, pause the deployment first |

In every case, keep the incident's own database (moved aside, the old branch, the old server):
it holds what was done after the backup, which the steps below need.

Keep each day's signed checkpoint outside the database. With `MENDARI_CHECKPOINT_KEY` set, the
server publishes it at `GET /api/audit/checkpoint`: fetch it daily, for example
`curl -s https://<host>/api/audit/checkpoint > checkpoint-$(date +%F).json` (`--against` reads
that answer as it is, or the statement alone), and keep the copies. Publish the public key
somewhere of your own as well.

After a restore, **before serving from it**:

1. Stop automation and sign everyone out on the restored database: its holds and sessions are
   the backup's, not the incident's.

   ```bash
   mendari automation hold --all-orgs --reason "restore, incident 7" --db "$RESTORE_URL"
   mendari access end-sessions --kind all --reason "restore, incident 7" --db "$RESTORE_URL"
   ```

2. Verify the whole chain against the last signed checkpoint from before the backup (pick it by
   date and by head), with the public key:

   ```bash
   mendari audit verify --db "$RESTORE_URL" --against checkpoint-2026-10-07.json --public-key <key>
   ```

   It fails if anything in the chain was changed, and also if the chain was rewritten from before
   that statement or cut short of it (`reason: statement`).
3. **Re-apply what was done after the backup.** A restore brings back everything undone since,
   in the unsafe direction too. List it from the incident's database (or, if that is gone, the
   security log for the window):

   ```bash
   mendari audit export --administrative --since <the backup's time> --db <the incident's database>
   mendari audit export --since <the backup's time> --db <the incident's database>
   ```

   Then, on the restored database:
   - keys and clients revoked (`key.revoked`, `client.revoked`): revoke them again
     (`mendari access revoke`);
   - keys rotated (`key.rotated`) and clients' key sets replaced (`client.keys_replaced`):
     revoke the old ones, and make or replace them again (the Access view);
   - automation approvals revoked (`governance.revoked`): revoke them again (the Decision Card);
   - memberships removed or roles lowered, invitations revoked: again (the Access view);
   - holds set: set them again;
   - inputs and notes erased on request (`input.erased`, `note.erased`): erase them again
     (`mendari phi erase --decision <id>`, `mendari notes erase --id <id>`, with the same
     reason), then `mendari phi purge --db "$RESTORE_URL"` for those past retention.
4. Record a checkpoint on it, **without** `MENDARI_CHECKPOINT_KEY` in that command's
   environment: `mendari audit checkpoint --db "$RESTORE_URL"`. A statement for today may
   already be published, and the next day's scheduled job signs the next one.
5. Point the deployment at it, then lift the restore's hold once the cause is understood.

Decisions made after the backup are lost with it, and so are their chain events. The
integrations that sent them get errors for the ids they hold, and the people who reviewed them no
longer see them. Every signed statement published after the backup no longer verifies against
the restored chain (`reason: statement`): record the restore point (the backup's time and the
restored head) and those statements' dates in the incident, and tell whoever keeps copies. Tell
the organizations affected.

Backups of a deployment in PHI mode `encrypt` hold its sealed inputs and notes: keep them as
protected as the database, delete them with its retention, and keep the PHI keys they were
sealed with until the last backup that needs them has expired
([Deploying with real patient data](../deploying-with-phi.md)).

The restore is tested in CI, on SQLite and on Postgres, with these commands
(`tests/test_restore.py`).

## Before deploying the chain's unique index

A start of this release gives `audit_events` a unique index on `prev_hash`
(`audit_events_prev_hash`), so the database itself refuses a second event chained after the same
one (a fork). A database that already holds a fork cannot take it: the start stops with an error
that says to run `mendari audit verify`, and migrates nothing. Before deploying it, check
production read-only: `mendari audit verify --db "$DATABASE_URL"` opens the database without
writing to it (no migration, no mark) and must say `"ok": true`. The fork check alone, in a
read-only transaction, must return no row:

```bash
psql "$DATABASE_URL" -c "BEGIN READ ONLY; SELECT 1 FROM audit_events GROUP BY prev_hash HAVING COUNT(*) > 1 LIMIT 1; ROLLBACK;"
```

The registry's chain gets the same index (`hub_events_prev_hash`), and its start the same
refusal, naming `mendari registry verify`. Check the registry's database likewise, with the
registry deployment's own `$DATABASE_URL`: `mendari registry verify --db "$DATABASE_URL"`
must pass, and its fork check must return no row:

```bash
psql "$DATABASE_URL" -c "BEGIN READ ONLY; SELECT 1 FROM hub_events GROUP BY prev_hash HAVING COUNT(*) > 1 LIMIT 1; ROLLBACK;"
```

If any of these fails, do not deploy: treat it as a SEV1 (the chain fails verification) and restore
from a backup taken before the break, as above.

## The firewall (the owner's)

The hosted deployment's per-address limit on `/auth/` and `/smart/` is in log mode
(`deploy/vercel/firewall-rules.json`). The owner enforces it after a week of logs. The results of
that week (dates, peak requests a minute per address, anything it would have refused) are
recorded here when they do.

## Afterwards

Write down what happened, when, what was done and what will change. Include the chain's head
before and after, and any signed statements a restore superseded. Lift holds only once the cause
is understood.
