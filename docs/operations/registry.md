# Running a pack registry

A **registry** keeps signed pack archives with immutable versions, and anyone can pull and
verify them. It shows only what it checked
([ADR-0012](../decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)): it renders
each version's Decision Card itself, never shows the card, benchmark or leaderboard a publisher
uploads, and answers what it verified (`checked`) apart from what the publisher says
(`claimed`). This page is for whoever runs a registry, and for a deployment that serves packs
pulled from one.

There is no public registry yet. Until LP-19 a registry has one administrator token, no
accounts and no namespace claims (`tasks/todo.md`). The docker sandbox runs one on port 8100.

## Starting a registry

```bash
export MENDARI_REGISTRY_ADMIN_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
export MENDARI_REGISTRY_SIGNING_KEY=$(openssl rand -base64 32)
mendari registry serve --db var/registry.db --port 8100     # a SQLite path or a postgresql:// URL
mendari registry verify --db var/registry.db                # read-only; exit 1 when broken
```

| Setting | Default | What it does |
|---|---|---|
| `MENDARI_REGISTRY_ADMIN_TOKEN` | unset: no administrator | The one administrator's bearer token: namespaces, tokens, publisher types, reviews, recalls and deletions. Keep it in a secret manager. |
| `MENDARI_REGISTRY_SIGNING_KEY` | unset: the registry recalls nothing itself | 32 random bytes in base64, the Ed25519 key that signs an administrator's recalls, published at `GET /v1/registry-key`. It is read when it is used, not at startup: after setting it, check that `GET /v1/registry-key` answers `200` with its id. |
| `MENDARI_REGISTRY_RATE_LIMITS` | `on` | `on`, `log` (count, never refuse) or `off`, then overrides such as `registry_write=240/h:40`. The defaults: reads 600 a minute per address (burst 200), writes 120 an hour per token (burst 30), site reports 60 an hour per token (burst 10). A token the registry did not issue counts against its sender's address. A bad value stops the registry. |
| `MENDARI_REGISTRY_REGRESSION` | `on` | `off` publishes without the regression check. |

Every answer carries a CSP that loads nothing, refuses framing, is never cached, and sends HSTS
but on this machine. A refusal by the limits is `429` with `Retry-After`, before the body is
read.

## What an administrator does

All of it is HTTP with `Authorization: Bearer $MENDARI_REGISTRY_ADMIN_TOKEN`; there is no
command for it. Each write is a chained event naming `admin` (see [The chain](#the-chain)).

| Action | Route | Body |
|---|---|---|
| Create a namespace | `POST /v1/namespaces` | `name`, `publisher_type` (`community` unless set), `display_name` |
| Set its publisher type, after checking who publishes there | `PATCH /v1/namespaces/{namespace}` | `publisher_type`: `community`, `verified` or `institutional` |
| Issue a publisher token (shown once, stored hashed) | `POST /v1/namespaces/{namespace}/tokens` | none |
| Review a version | `POST /v1/packs/{namespace}/{name}/{version}/review` | `risk_level` (0 to 4) found, and a `note` of one printable line, at most 300 characters, never case text |
| Recall a version | `POST /v1/packs/{namespace}/{name}/{version}/yank` | `reason` (one line, at most 300) and `severity` (`low`, `medium`, `high`, `critical`); signed with `MENDARI_REGISTRY_SIGNING_KEY` (`409` without it) |
| Delete a version that must not be served | `POST /v1/packs/{namespace}/{name}/{version}/delete` | `reason` |

A publisher, with its token, registers its keys (`mendari keys register`), publishes
(`mendari push`) and recalls its own versions (`mendari yank`, signed with one of its keys). A
review shows in `checked.review` and `claimed.risk.confirmed`; the newest review of a version
is the one shown. A review's note stays in the chain for good.

## Recalls and deletions

A recall (`mendari.yank/1`) names one version exactly (pack, version, digest, archive), with a
reason, a severity, who signed it and when. A version is recalled once. A recalled version
leaves the latest version, search and the namespace's list; it is still served by its exact
version, with the record, and its archive stays. `GET /v1/yanks` lists every recall.

A deletion is for a version that must not be served at all: its archive answers `410` with the
reason, its bytes go once no version that is not deleted needs them, and its row and a
tombstone stay, so the version is never published again. `mendari pull` refuses a deleted
version and removes a cached copy.

### When a pack must stop

1. **Recall it.** The publisher runs
   `mendari yank ns/name@1.2.0 --reason "misroutes refills" --severity high --key keys/publisher.key`,
   or the administrator posts to `…/yank` with a reason and a severity.
2. **Servers stop automating it, from their next read.** A server reads `GET /v1/yanks` of a
   registry its served packs came from in a thread of its own. A decision for one of that
   registry's packs starts the read, once the last one is older than
   `MENDARI_REGISTRY_RECALL_CHECK_S` (300 s unless set); that decision, and any made while the
   read runs, use the list read before. From then on a recalled pack no longer automates, and
   its own rule's BLOCK goes to a person, whether or not its organization approved it, and each
   held decision says why in its reasons. A server that decides all day has the recall within
   about five minutes; on a quiet one, the first decision after the recall can still automate.
   The server's full `/api/health` (signed in, or in open mode) shows, under `registry_recalls`,
   each registry's last read (`checked_s_ago`, which grows while no decision starts a read), how
   many versions it recalls (`recalled`) and the error type while reads fail (`failing`). The
   Decision Card shows a recall the last pull recorded; one read since shows in health and in
   the decisions' reasons.
3. **Not soon enough, or a quiet server?** Hold the pack on each deployment now
   ([Holding automation](automation-hold.md)): a hold takes effect within seconds, with no read.
4. **Afterwards**, remove the recalled version from each server's pack cache
   (`packs/<namespace>/<name>/<version>` under `MENDARI_CACHE_DIR`), or pull a fixed version.
   A new version has a new digest, so it waits for its organization's approval.
5. **If it must never be served,** delete it as well.

A recall a server cannot verify is still honoured: it only lowers automation. A serverless
instance starts unread and holds its pulled packs until its first read. `mendari decide` and
`Decision.from_hub` know only the recall the last pull recorded.

## Serving pulled packs

A pack pulled from a registry never automates until your organization approves its exact
digest, at every risk level (2 and 3 need their governance approval too, and 4 never
automates); until then its own rule's BLOCK goes to a person. Approve it on its Decision Card
(the Automation approval section: where it came from, the digest served, any recall, then a
dialog naming the digest) or with `POST /api/registry-approvals` (`pack_id`, `pack_digest`, a
`reference` such as the committee's minutes); `DELETE /api/registry-approvals/{id}` revokes it
at once. Both need the `approve` permission and are chained. An approval names one digest and
lasts until revoked. It never lifts a recall, a hold or a risk level's cap.

| Setting | Default | What it does |
|---|---|---|
| `MENDARI_REGISTRY_RECALL_CHECK_S` | 300 (1 to 604800 seconds) | How old a server's last read of a registry's recalls may be before a decision for one of its packs starts another (that decision uses the last list) |
| `MENDARI_REGISTRY_RECALL_STALE_S` | 86400 (at least twice the check) | After this long without a successful read, every pack from that registry is held |

These reads go only to the registry a pack's verified marker names, over https (plain http to
this machine), with no redirects, 5 s per step, 20 s for the whole answer and 5 MB at most.
They are not under `MENDARI_PROVIDER_ALLOWED_ORIGINS`. A pack pulled before LP-18d names no
registry and is held until it is pulled again.

A pulled pack is shown with the registry's card. Its uploaded benchmark is never a reference
here: the Monitoring view says it is not compared (`benchmark.reason: from_registry`) and runs
only the checks that need no benchmark, and an evaluation says it is not compared with the
benchmark the pack carries. Give `mendari eval --baseline report.json` a report of your own to
compare with.

## The regression check

On every publish the registry evaluates the version on its own test set
(`evaluation.benchmark_dataset`), in a child process with no credential, local providers only
and 180 s at most, and compares it with the newest earlier version's check (the pack gate). It
shows the result as `checked.regression`, labelled "publisher's own test set: regression check,
not a ranking", and never refuses a version for it. The publisher chose the set, so it can show
a regression, never rank. A pack whose default providers call a model is not measured
(`needs_remote`). At most two checks run at once; a publish past that is told to try again
(`503`).

## The chain

Every write (a namespace, its publisher type, a token, a key, a publish, a site report, a
review, a recall, a deletion) appends a hash-chained event, naming who acted: `admin`, or a
token by its id (`token:<id>`), never the token. `mendari registry verify --db` opens the
database read-only and checks every event's hash and order, that every row is exactly the one
its newest event hashed, every archive's hash and every key's id. It prints the head.

The chain is unkeyed: whoever can write the whole database can rewrite it. Keep its head
(`GET /v1/chain/head`: the event count and the last hash) somewhere the registry cannot write,
on a schedule, and compare a later head with it. A signed head is for later.

## Not yet

- **Accounts and claims (LP-19).** One shared administrator token; publisher tokens have no
  owner, expiry or revocation; a first publish to a new namespace is listed at once. The
  registry is to run under the engine app (LP-19's option A, the default the owner confirmed on
  2026-11-07, [ADR-0012](../decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)).
  LP-19's build is next; its deploy, the registry's signing key and DNS are the owner's.
- **Site reports** are served marked `verified: false` until sites sign them (LP-22); the Hub
  shows none.
- **The registry's signing key** has no rotation path; clients pin it at first contact.
- **`GET /v1/yanks` is not paged.**

The threat model's rows S18, S19, T7, R3, D11, D12, E12 and E13 hold the registry's checks and
the tests that prove them ([threat model](../security/threat-model.md)).
