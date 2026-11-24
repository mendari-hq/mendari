#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Mendari's scheduled jobs on one machine (docker-compose.yml, service `jobs`), in the
# database's own image: a POSIX shell, pg_dump of the database's version, and busybox.
#
#   jobs.sh daily                  every job now, then once a day, for as long as it runs
#   jobs.sh once                   every job once; exit 1 when one failed
#   jobs.sh backup                 a backup now; exit 1 when it failed
#   jobs.sh forks                  exit 1 when the audit trail holds a fork; read-only
#   jobs.sh restore-into DB FILE   a new database DB holding the backup FILE (in /backups)
#   jobs.sh swap DB KEPT           DB takes the live database's place, which is kept as KEPT
#   jobs.sh drop DB                drop a database a restore made
#
# The jobs, in order:
#   1. retention: delete kept inputs and notes past their retention.
#   2. checkpoint: verify the audit trail from its last checkpoint and record the next one.
#   3. monitor: check every agent for drift, and send the alerts still waiting.
#   4. backup: pg_dump the database, read the file back, drop the backups past their days.
#
# The first three are the engine's own scheduled routes, called with the jobs' secret: the
# engine records each run (the System view shows them), and it is the engine that tells the
# alert webhook when the audit trail does not verify. Nothing here is passed over: a job that
# fails is said on stderr, the run ends "failed" (the container turns unhealthy until a run
# passes), and `once` and `backup` exit 1.
#
# The secret is in this container's process list while a route is called. It is of use only on
# this machine's own Docker network: the front refuses /api/internal/ from outside (proxy.conf).

set -u

ENGINE_URL=${ENGINE_URL:-http://engine:8000}
BACKUPS=${BACKUPS:-/backups}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}
STATUS=$BACKUPS/.jobs-status
LAST_RUN=$BACKUPS/.jobs-last-run
DAY=86400
# A run asked for less than this long after the last one waits for the day to pass: a restart
# of the machine does not run everything again, and never sends an alert twice.
SETTLED=72000
# How long one route may take: the monitor is given three minutes by the engine itself.
PATIENCE=900

failed=0

say() {
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

problem() {
    say "FAILED: $*" >&2
    failed=1
}

# call METHOD PATH: the engine's answer to a scheduled route on stdout, asked with the jobs'
# secret. It fails unless the engine answered that the request went well.
call() {
    if [ "$1" = POST ]; then
        wget -q -T "$PATIENCE" -O - --post-data '' \
            --header "Authorization: Bearer $CRON_SECRET" "$ENGINE_URL$2"
    else
        wget -q -T "$PATIENCE" -O - --header "Authorization: Bearer $CRON_SECRET" "$ENGINE_URL$2"
    fi
}

wait_for_engine() {
    tries=0
    until wget -q -T 5 -O /dev/null "$ENGINE_URL/api/health"; do
        tries=$((tries + 1))
        if [ "$tries" -ge 60 ]; then
            problem "the engine did not answer at $ENGINE_URL"
            return 1
        fi
        sleep 5
    done
}

retention() {
    if call GET /api/internal/retention >/dev/null; then
        say "retention: kept inputs and notes past their retention were deleted"
    else
        problem "retention: kept inputs past their retention were not deleted"
    fi
}

checkpoint() {
    if ! body=$(call GET /api/internal/checkpoint); then
        problem "checkpoint: the audit trail was not checked"
        return
    fi
    case $body in
    *'"ok":true'*)
        say "checkpoint: the audit trail verifies from its last checkpoint"
        ;;
    *)
        problem "checkpoint: THE AUDIT TRAIL DOES NOT VERIFY. The engine has told the alert" \
            "webhook, if one is set. Hold all automation, keep the database as it is, and" \
            "follow docs/operations/incident-runbook.md"
        ;;
    esac
}

monitor() {
    if ! body=$(call GET /api/internal/monitor); then
        problem "monitor: the agents were not checked for drift"
        return
    fi
    case $body in
    *'"not_delivered":0'*)
        say "monitor: every agent was checked for drift"
        ;;
    *)
        problem "monitor: an alert could not be delivered to the alert webhook"
        ;;
    esac
}

# report OUTCOME: tell the engine how the backup ended, for the System view.
report() {
    call POST "/api/internal/backup?outcome=$1" >/dev/null ||
        say "the backup's outcome ($1) could not be reported to the engine" >&2
}

backup() {
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    partial=$BACKUPS/.mendari-$stamp.partial
    kept=$BACKUPS/mendari-$stamp.dump
    umask 077
    # Written beside its name and read back before it takes it: a backup that is there is whole.
    if pg_dump --format=custom --file "$partial" && pg_restore --list "$partial" >/dev/null; then
        mv "$partial" "$kept"
        say "backup: $(basename "$kept")"
        find "$BACKUPS" -maxdepth 1 -name 'mendari-*.dump' -mtime "+$KEEP_DAYS" -exec rm -f {} \;
        report ok
    else
        rm -f "$partial"
        problem "backup: the database could not be backed up, or the backup could not be read back"
        report failed
    fi
}

run_all() {
    failed=0
    say "the daily jobs start"
    if wait_for_engine; then
        retention
        checkpoint
        monitor
    fi
    backup
    date +%s >"$LAST_RUN"
    if [ "$failed" -eq 0 ]; then
        printf 'ok\n' >"$STATUS"
        say "the daily jobs passed"
    else
        printf 'failed\n' >"$STATUS"
        say "the daily jobs FAILED: read the lines above" >&2
    fi
    return "$failed"
}

# How long until the next run: a day after the last one, at once when none is on record.
until_next() {
    last=$(cat "$LAST_RUN" 2>/dev/null || printf 0)
    case $last in '' | *[!0-9]*) last=0 ;; esac
    since=$(($(date +%s) - last))
    if [ "$since" -ge 0 ] && [ "$since" -lt "$SETTLED" ]; then
        printf '%s\n' "$((DAY - since))"
    else
        printf '0\n'
    fi
}

# A database's name as a restore makes one: nothing a statement could misread.
database_name() {
    case $1 in
    '' | *[!a-z0-9_]*)
        say "not a database name a restore makes: $1" >&2
        exit 2
        ;;
    esac
}

case ${1:-daily} in
daily)
    pause=$(until_next)
    if [ "$pause" -gt 0 ]; then
        say "the daily jobs ran $((DAY - pause)) s ago: the next run is in $pause s"
        sleep "$pause"
    fi
    while :; do
        run_all || true
        sleep "$DAY"
    done
    ;;
once)
    run_all
    ;;
backup)
    backup
    [ "$failed" -eq 0 ]
    ;;
forks)
    # Whether two events were ever chained after the same one, asked in a read-only
    # transaction: a version whose start gives the chain its unique index cannot take a
    # database that holds such a fork (docs/operations/incident-runbook.md).
    found=$(psql -qAt -v ON_ERROR_STOP=1 -c "BEGIN READ ONLY; SELECT 1 FROM audit_events GROUP BY prev_hash HAVING COUNT(*) > 1 LIMIT 1; ROLLBACK;") || {
        say "the audit trail could not be read for forks" >&2
        exit 1
    }
    if [ -n "$found" ]; then
        say "THE AUDIT TRAIL HOLDS A FORK: two events are chained after the same one" >&2
        exit 1
    fi
    say "no fork in the audit trail"
    ;;
restore-into)
    database_name "${2:-}"
    case ${3:-} in
    mendari-*.dump) ;;
    *)
        say "a backup is named mendari-<time>.dump" >&2
        exit 2
        ;;
    esac
    psql -q -v ON_ERROR_STOP=1 -d postgres -c "CREATE DATABASE \"$2\"" &&
        pg_restore --no-owner --exit-on-error --dbname "$2" "$BACKUPS/$3"
    ;;
swap)
    database_name "${2:-}"
    database_name "${3:-}"
    psql -q -v ON_ERROR_STOP=1 -d postgres \
        -c "ALTER DATABASE \"$PGDATABASE\" RENAME TO \"$3\"" \
        -c "ALTER DATABASE \"$2\" RENAME TO \"$PGDATABASE\""
    ;;
drop)
    database_name "${2:-}"
    psql -q -v ON_ERROR_STOP=1 -d postgres -c "DROP DATABASE IF EXISTS \"$2\""
    ;;
*)
    say "usage: jobs.sh daily | once | backup | forks | restore-into DB FILE | swap DB KEPT | drop DB" >&2
    exit 2
    ;;
esac
