# Running a pack registry

A **registry** keeps signed pack archives with immutable versions, and anyone can pull and
verify them. It shows only what it checked
([ADR-0012](../decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)): it renders
each version's Decision Card itself, never shows the card, benchmark or leaderboard a publisher
uploads, and answers what it verified (`checked`) apart from what the publisher says
(`claimed`). This page is for whoever runs a registry, and for a deployment that serves packs
pulled from one.

There is no public registry yet. Until LP-19 is finished a registry has one administrator
token and no accounts (`tasks/todo.md`); namespace claims are made and decided with the
operator's commands ([Namespace claims](#namespace-claims)). The docker sandbox runs one on port 8100.

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
| `MENDARI_REGISTRY_SIGNING_KEY` | unset: the registry recalls nothing itself and counter-signs nothing | 32 random bytes in base64, the Ed25519 key that signs an administrator's recalls and counter-signs each version the registry lists ([below](#the-counter-signature)), published at `GET /v1/registry-key`. It is read when it is used, not at startup: after setting it, check that `GET /v1/registry-key` answers `200` with its id. |
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

## Reaching a registry

The client (`mendari login`, `pull`, `push`, `search`, `yank`, `Decision.from_hub`) reaches a
registry over https only, and refuses any other address before it sends anything, so a
publisher token never crosses a network in clear. Plain http is allowed to this machine alone:
`localhost`, a `.localhost` name or a loopback address, which is what a local registry and the
docker sandbox use. An address with a user or a password in it is refused. Put a registry that
other machines use behind TLS.

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

## Publisher tokens that belong to someone

A publisher token made with `mendari registry token` belongs to an organization, ends, and can
be revoked. It is for a registry nobody signs in to: one you host yourself, CI, a seed script.
The commands write to the registry's database, so they work while the registry runs, and the
running registry refuses a revoked or expired token at its next request. The database must
already be there (`mendari registry serve` makes it), and the namespace must exist.

```bash
mendari registry token --db var/registry.db --namespace acme --org acme \
  --reason "the release runner" --days 90          # prints the token once, with its id
mendari registry tokens --db var/registry.db       # read-only; never a secret
mendari registry revoke-token --db var/registry.db ID --reason "the runner was retired"
```

- A token lives 1 to 365 days (90 unless `--days` says otherwise) and publishes only to its
  namespace. The registry keeps its SHA-256, never the token: a lost token is revoked and
  replaced, not recovered.
- Its owner is recorded as `operator`, with who ran the command and why, in the chain
  (`token.issued`); revoking is chained too (`token.revoked`).
- `tokens` shows each token's organization, owner, name, expiry, when and by whom it was
  revoked, and the last day it was used (in the deployment's time zone). That day is kept
  outside the chain; revoke a token nobody uses.
- A token an administrator issued over HTTP (`POST /v1/namespaces/{namespace}/tokens`) has no
  owner or expiry and cannot be revoked. Those keep working for now; LP-19g ends them.

## Namespace claims

A claim links a namespace to an organization. It is `pending` until it is approved or refused,
once, and until it is approved nothing of the namespace is served to anyone but its own token
and a reviewer: to everyone else it answers as a namespace that does not exist (the same `404`),
in every read, search, the recall list and health's pack count. A namespace with no claim, such
as one an administrator made over HTTP or one from before claims, is listed as before.

```bash
mendari registry claim --db var/registry.db --namespace acme --org acme \
  --display-name "Acme Health" --reason "asked by its administrator"
mendari registry token --db var/registry.db --namespace acme --org acme --reason "first publish"
# the publisher registers a key and publishes (mendari keys register, mendari push), then:
mendari registry approve --db var/registry.db --namespace acme --reason "checked the publisher"
mendari registry refuse  --db var/registry.db --namespace acme --reason "a lookalike"
mendari registry claims  --db var/registry.db [--org ORG] [--status pending]   # read-only
```

- **Reserved names.** `mendari`, `openhealth` and any name that starts with either and a dash
  are claimed with this command only. Any other lookalike is yours to refuse.
- **A name is claimed once.** A name that exists is refused (`409`), whatever its claim's
  state; a refused namespace stays taken. Releasing a name is not built.
- **Three pending claims** per organization at most. A decided claim no longer counts.
- **Approval needs a published version** that is not deleted (`409` without one): the claim is
  decided on its first publish. With a pending claim the publisher registers keys and
  publishes as usual, the regression check runs, and its own token reads what it published
  (`listed: false` on the namespace and on each version), so `mendari push` confirms.
- **Approved**, the namespace's later versions and new pack names are listed at once. A
  decision is final: an approved claim is not refused later (recall or delete a version
  instead), and a refused one is never approved.
- **Refused**, nothing is removed: its versions and archives stay in the database, served only
  to its own token and a reviewer, and its token reads the note.
- Each step is chained with the organization, who ran the command and why (`namespace.created`
  and `namespace.claimed`, then `namespace.approved` or `namespace.refused`), and every
  `pack.published` records the organization and whether the version was listed then.
- Until reviewers sign in (LP-19e) the administrator token reads everything, as a reviewer.

## The counter-signature

With `MENDARI_REGISTRY_SIGNING_KEY` set, the registry signs a record for each version when it
becomes listed (`mendari.registry-acceptance/1`: the pack, version, digest, archive SHA-256, the
publisher key that signed the archive, the registry's key id and the time). It does so at
publish in a listed namespace, and at approval for every version published while the claim was
pending. A version of a refused namespace is never counter-signed, nor a deleted one. The record
is served with the version as `checked.accepted` (`null` when there is none), and the Hub shows
"Counter-signed by the registry" or "Not counter-signed".

A version written straight into the database has no record. `mendari pull` checks the record
before it fetches the archive:

| The registry | The version's record | The pull |
|---|---|---|
| publishes a key this machine trusts | verifies, and names exactly this pack, version, digest, archive and publisher key | pulled; the marker beside the pack records `accepted: true` |
| publishes a key this machine trusts | missing, does not verify, or names something else | **refused**, nothing cached |
| publishes no key, and never did to this machine | (none) | pulled as before; `accepted: null`, and `mendari pull` says it is not counter-signed |
| published a key before, and now publishes none or another | (any) | **refused**, as a changed host key is |

A machine trusts the registry's key when it is one shipped with the client
(`src/mendari/hub/registry_keys.json`, public keys only, never a hostname) or the one it pinned
for that registry's URL on first use (`trusted_keys.json` in `~/.config/mendari`, the
`@registry` entry). The shipped file is empty until the registry's owner makes the key and sends
its public half (`GET /v1/registry-key`) for the one-line change that ships it.

Turning the key on for a registry that already holds versions refuses those versions to every
client until they are counter-signed. Do both together:

```bash
export MENDARI_REGISTRY_SIGNING_KEY=...                 # the same key the registry serves with
# first: the chain must verify, and still hold the last checkpoint you kept outside the database
mendari registry verify --db var/registry.db --checkpoint /elsewhere/registry-checkpoint.json
# signs the listed versions with no record, and nothing if the chain no longer holds that head
mendari registry countersign --db var/registry.db --checkpoint /elsewhere/registry-checkpoint.json
mendari registry verify --db var/registry.db --checkpoint /elsewhere/registry-checkpoint.json
```

Before the registry has a signing key there is no checkpoint to give: take the first one
(`mendari registry checkpoint`, [the chain](#the-chain)) as soon as the key is set, compare the
head `verify` prints with any copy of `GET /v1/chain/head` you kept by hand, and only then
counter-sign.

`countersign` signs after the fact, so it is the one step that could put the registry's
signature on a version it never accepted. It therefore:

- refuses to run, signing nothing, when the chain does not verify (a planted, changed or removed
  row);
- with `--checkpoint FILE` (a statement `mendari registry checkpoint` signed earlier, kept
  outside the database, [the chain](#the-chain)), also refuses when the chain no longer holds
  the head the statement names, or when the statement is not one this registry's key signed.
  The chain is unkeyed, so only that copy shows a chain rewritten whole. Run without one, it
  says so (`"checkpoint": null` in what it prints, and a line on standard error): the chain
  verified, and a chain rewritten whole would have too;
- checks each version again from what is stored before it signs: its archive is there and
  hashes to its name, is signed by a key of its namespace, and its signed manifest names this
  pack, version and digest. One that fails is skipped and printed with why, and the command
  exits 1. An approval makes the same check on the versions published while the claim was
  pending;
- prints the versions it signed, chains each record (`pack.accepted`) with who ran it, and signs
  nothing the second time. It needs the key, and never creates a database.

Run it again after any stretch in which the registry served without its key. A signing key that
is set and is not a key (not 32 bytes in base64) stops publishing, approval and `countersign`
with an error that names the setting and never its value (`503` over HTTP).

Known limits, stated plainly:

- **A shipped key does not make a signature required, unless the machine asks for it.** By
  default a machine that has no pin for a registry's address accepts, on first contact, a
  registry that shows no key (an unsigned pull, `accepted: null`) or shows any valid key
  (pinned on first use), before the shipped keys count for anything. Shipping the key removes
  the first-use step for the real registry; it does not stop a first contact with an impostor.
  Set `MENDARI_REGISTRY_COUNTERSIGNATURE` on the machine that pulls to close that:

    | Value | A pull is refused when |
    |---|---|
    | `optional` (unset) | only as the table above says |
    | `required` | the registry shows no key: every pull is counter-signed by a shipped or pinned key |
    | `shipped` | the registry's key is not one shipped with the client: nothing is trusted on first use |

    Use `shipped` on any machine that pulls from the hosted registry once its key ships with
  the client, and `required` for a self-hosted registry that has a key. A value that is not one
  of the three refuses every pull.
- **A cached copy is not re-checked when it is loaded.** `Decision.from_hub` and the server
  load a pack already in the cache without asking the registry, so a copy pulled while the
  registry had no key (`accepted: null`) keeps loading after the registry gets one. The check
  runs at `mendari pull`.
- **No freshness and no revocation.** A record has no expiry and is never withdrawn: a deleted
  version's record still verifies (the deletion is what stops it being served), and someone who
  can write the database can serve an older accepted version as the latest. Recalls
  ([above](#recalls-and-deletions)) are the way a version is stopped.
- **Whoever can write the database can still plant a namespace key** and an archive it
  signed, and the backfill would then sign that version. If they rewrote the chain to do it, a
  checkpoint from before, given with `--checkpoint`, refuses the run. If they appended to the
  chain instead (it is unkeyed, so they can), the checkpoint still holds and nothing refuses:
  `verify --checkpoint` prints how many events came after the checkpoint, and those are yours
  to read before you counter-sign. A checkpoint taken after the plant vouches for it, so take
  them on a schedule, not just before a backfill. Run without `--checkpoint`, nothing shows a
  rewrite.
- A machine's pins are filed by the registry's address (scheme and host in lower case, no
  default port). A pin file that is there and cannot be read is refused, with what to remove,
  rather than read as empty.

Changing the key is not supported yet, by decision for launch. Each version holds one record,
signed with the key of the day, and no command signs it again: after a change, every record made
before it stops verifying, and every machine that pinned the old key refuses the registry. The
pieces a rotation will use are already there (the shipped file is a list, and a shipped key is
trusted whatever a machine pinned, so a new key can ship beside the old one before the switch);
the missing piece is a chained command that signs the existing versions again. Until it exists,
treat the key as permanent and keep it where only the registry's environment reads it.

## The chain

Every write (a namespace, its claim and its decision, its publisher type, a token made or revoked, a
key, a publish, a counter-signature, a site report, a review, a recall, a deletion) appends a hash-chained event, naming who acted: `admin`, or a
token by its id (`token:<id>`), never the token. `mendari registry verify --db` opens the
database read-only and checks every event's hash and order, that every row is exactly the one
its newest event hashed, every archive's hash and every key's id. It prints the head.

The chain is unkeyed: whoever can write the whole database can rewrite it, and it then
verifies. What shows a rewrite is a head kept somewhere the registry cannot write. With a
signing key (`MENDARI_REGISTRY_SIGNING_KEY`) the registry signs one for you:

```bash
# once a day, from a machine that reads the database and holds the key; keep every file
mendari registry checkpoint --db var/registry.db \
  --previous /elsewhere/registry-checkpoint.json > /elsewhere/registry-checkpoint.new.json \
  && mv /elsewhere/registry-checkpoint.new.json /elsewhere/registry-checkpoint.json
# whenever you check the registry, and before every countersign
mendari registry verify --db var/registry.db --checkpoint /elsewhere/registry-checkpoint.json
```

`mendari registry checkpoint` prints a `mendari.registry-checkpoint/1` statement: the head, the
day (UTC), the key's id and an Ed25519 signature over those and the format. The format is the
statement's own, so a recall, a counter-signature, the engine's audit checkpoint
(`mendari.checkpoint/1`) or a scoreboard result signed by the same key is never read as one.
The command opens the database read-only and stores nothing in it (a copy there could be
rewritten with the chain); it never makes a missing database. It refuses to sign when the chain
does not verify and, with `--previous`, when the chain no longer holds the last statement's
head, so a rewrite stops the routine instead of being signed over. The first run has no
`--previous`. Keep the files where no credential of the registry can write: another account's
bucket with versioning, a repository, a mailbox. Keep the dated copies too if you want to know
since when.

`mendari registry verify --checkpoint FILE` checks the statement's signature with the
registry's key (`MENDARI_REGISTRY_SIGNING_KEY`, or `--public-key` with the public key `GET
/v1/registry-key` shows, for someone who does not hold the private one), then the chain, then
that the head the statement names is an event of the chain. A chain rewritten or cut short from
before the checkpoint fails (exit 1), where `verify` alone passes. It prints how many events
were appended since the checkpoint.

What a checkpoint does not show:

- **Anything appended after it.** A writer of the database can append events as well as rewrite
  them. The newest checkpoint bounds what they could have changed unseen to the events since.
- **A rewrite made before the first checkpoint**, or between a plant and the next checkpoint
  when nobody gave `--previous`: a statement signed over a rewritten chain vouches for it.
- **Anything, to someone who also holds the key.** The key lives in the registry's environment;
  whoever has it can sign a statement for any head. Keeping the statements elsewhere, with their
  dates, is what makes a replaced one visible.

Without a signing key, keep the unsigned head (`GET /v1/chain/head`: the event count and the
last hash) the same way and compare by hand.

## Not yet

- **Accounts (LP-19).** One shared administrator token; people cannot yet sign in to claim a
  namespace, make their own publisher tokens or review a claim (the operator's commands do,
  above). A namespace the administrator token makes over HTTP has no claim and is listed at
  once, until LP-19g ends that token. The
  registry is to run under the engine app (LP-19's option A, the default the owner confirmed on
  2026-11-07, [ADR-0012](../decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)).
  LP-19's build is next; its deploy, the registry's signing key and DNS are the owner's.
- **Site reports** are served marked `verified: false` until sites sign them (LP-22); the Hub
  shows none.
- **The registry's signing key** has no rotation path; clients pin it at first contact, or trust
  it because it ships with them (the shipped file is empty until the owner makes the key).
- **`GET /v1/yanks` is not paged.**

The threat model's rows S18, S19, T7, T11, R3, D11, D12, E12 and E13 hold the registry's checks and
the tests that prove them ([threat model](../security/threat-model.md)).
