# Running a pack registry

A **registry** keeps signed pack archives with immutable versions, and anyone can pull and
verify them. It shows only what it checked
([ADR-0012](../decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)): it renders
each version's Decision Card itself, never shows the card, benchmark or leaderboard a publisher
uploads, and answers what it verified (`checked`) apart from what the publisher says
(`claimed`). This page is for whoever runs a registry, and for a deployment that serves packs
pulled from one.

There is no public registry yet. Until LP-19 is finished a registry of its own has one
administrator token and no accounts (`tasks/todo.md`); namespace claims are made and decided
with the operator's commands ([Namespace claims](#namespace-claims)). A registry can also run
under the engine, where people sign in to claim and mint ([Under the engine](#under-the-engine)). The docker sandbox runs one on port 8100.

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
| `MENDARI_REGISTRY_RATE_LIMITS` | `on` | `on`, `log` (count, never refuse) or `off`, then overrides such as `registry_write=240/h:40`. The defaults: reads 600 a minute per address (burst 200), writes 120 an hour per namespace (burst 30; all of a namespace's tokens share it), site reports 60 an hour per namespace (burst 10). A token the registry did not issue counts against its sender's address. A bad value stops the registry. |
| `MENDARI_REGISTRY_REGRESSION` | `on` | `off` publishes without the regression check. |

Every answer carries a CSP that loads nothing, refuses framing, is never cached, and sends HSTS
but on this machine. A refusal by the limits is `429` with `Retry-After`, before the body is
read.

## Under the engine

`MENDARI_REGISTRY=on` serves the registry from the engine itself, on the engine's database, at
`/registry/v1/...` (so a client's registry address is the engine's, with `/registry`). It is
off by default: unset or `off`, the engine mounts nothing, makes no registry table and answers
those paths as it did before. Any other value stops the server. Turning it on puts publisher
bytes and reads that need no sign-in on the engine's origin and database: it is the owner's
decision for a deployment, and no release turns it on.

- **Reads need no sign-in**, as on a registry of its own, and carry the same headers: the CSP
  that loads nothing, `nosniff`, `no-store`, and HSTS but on this machine. An archive is sent as
  an attachment (`Content-Disposition: attachment`). The registry serves no page of its own
  there; its routes are in the engine's API reference (`/docs/reference`, tag *Registry*).
- **Publishing is by publisher token only** (`Authorization: Bearer ohr_…`). The engine's
  guard reads no credential under `/registry/`, and the registry reads no session cookie: a
  signed-in browser on any page publishes nothing. An engine API key (`ohk_`) or access token
  is refused by the registry like any unknown token, and a publisher token is refused by the
  engine everywhere else.
- **No administrator token.** `MENDARI_REGISTRY_ADMIN_TOKEN` is not read under the engine: a
  shared secret that publishes to every namespace has no place on the engine's origin. Until
  registry reviewers sign in (LP-19e), claims are approved and refused, and versions reviewed,
  recalled by the registry and deleted, with the operator's commands on the database
  ([Namespace claims](#namespace-claims)).
- **People claim and mint in the engine**, with sign-in on. An organization's administrators
  (the `keys` permission; never a key, a client or a publisher token) use:

  | Route | What it does |
  |---|---|
  | `GET /api/registry/namespaces` | The organization's claims: status, whether listed, publisher keys by id |
  | `POST /api/registry/namespaces` `{name, display_name}` | Claim a namespace (`pending`; three pending at most) |
  | `POST /api/registry/namespaces/{namespace}/keys` `{public_pem}` | Register a publisher key |
  | `GET /api/registry/tokens` | The organization's publisher tokens, never a secret |
  | `POST /api/registry/tokens` `{namespace, name, days}` | Mint a token (shown in this answer only; 90 days unless asked, 365 at most) |
  | `DELETE /api/registry/tokens/{id}` | Revoke it, at once |

  Each acts for the organization the person is signed in to, never one a body names: another
  organization's namespace or token answers `404`, as one that does not exist. Another site's
  page cannot call them (the engine's origin check). Each write is chained in the registry's
  chain with the organization and the person's id (`user:<hash>`, never an address), and
  logged (`admin.registry_namespace.claimed`, `admin.registry_key.added`,
  `admin.registry_token.created|revoked`) with ids only. With sign-in off these routes are not
  mounted: the operator's commands claim and mint. A namespace has 20 working tokens and 20
  publisher keys at most through these routes, and an organization mints 50 tokens a day at
  most (`409`, `429`).
- **A request body is the engine's limit at most** (`MENDARI_MAX_BODY_BYTES`, 4 MiB by default;
  a host may allow less): a larger archive is a `413` that says so. A registry of its own takes
  100 MB. Larger archives under the engine wait for the pack store (LP-20).
- **Limits and logs.** The registry's own rate limits apply (`MENDARI_REGISTRY_RATE_LIMITS`,
  above), counted in the engine's database. Requests under `/registry/` get an access line in
  the engine's security log with the route's template (`/registry/v1/packs/{namespace}/{name}`),
  never the raw path or a token.
- **Two chains, one database.** The registry's events stay in its own chain (`hub_events`,
  `mendari registry verify --db`), the engine's in the audit chain; neither is written twice.
  The registry's events are not in the Access view's Activity tab.
- The engine does not close the function early for a long publish: the regression check runs
  within the host's request time limit.

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
   many versions it recalls (`recalled`), the error type while reads fail (`failing`) and
   whether it publishes a signing key (`signs`; see the known limits below). The
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
mendari registry verify --db var/registry.db            # first: the chain must verify
export MENDARI_REGISTRY_SIGNING_KEY=...                 # the same key the registry serves with
mendari registry countersign --db var/registry.db       # signs the listed versions with no record
mendari registry verify --db var/registry.db            # and again after
```

`countersign` signs after the fact, so it is the one step that could put the registry's
signature on a version it never accepted. It therefore:

- refuses to run, signing nothing, when the chain does not verify (a planted, changed or removed
  row). Run `verify` first, and compare its head with the copy you keep outside the database
  ([the chain](#the-chain)): the chain is unkeyed, so only that copy shows a chain rewritten
  whole;
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
- **A cached copy is re-checked by a running server, not by the command line.** A server
  asks each registry its packs came from whether it publishes a signing key, in the same read
  as its recalls ([above](#recalls-and-deletions)). Once the registry has one, a copy pulled
  while it had none (`accepted: null`), or before counter-signatures, stops automating: its
  decisions go to a person with a reason that says to pull it again, and `mendari pull` of the
  same version checks the counter-signature and lets it automate again, with no restart. The
  copy stays in the cache. A registry the server cannot read changes nothing new (what was
  known stands until it is stale, as for recalls). `Decision.from_hub` and `mendari decide`
  still load a cached copy without asking the registry, as before: the check for them runs at
  `mendari pull`.
- **No freshness and no revocation.** A record has no expiry and is never withdrawn: a deleted
  version's record still verifies (the deletion is what stops it being served), and someone who
  can write the database can serve an older accepted version as the latest. Recalls
  ([above](#recalls-and-deletions)) are the way a version is stopped.
- **Whoever can write the database and has the chain's head rewritten can still plant a
  namespace key** and an archive it signed; the backfill would then sign that version. The head
  kept outside the database is what catches it.
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

The chain is unkeyed: whoever can write the whole database can rewrite it. Keep its head
(`GET /v1/chain/head`: the event count and the last hash) somewhere the registry cannot write,
on a schedule, and compare a later head with it. A signed head is for later.

## Not yet

- **Accounts (LP-19).** Under the engine, an organization's administrators claim namespaces
  and make publisher tokens ([Under the engine](#under-the-engine)); nobody yet signs in to
  review a claim (LP-19e: the operator's commands do, above), and the dashboard has no page for
  any of it (LP-19f). A registry of its own still has one shared administrator token, and a
  namespace that token makes over HTTP has no claim and is listed at once, until LP-19g ends
  it. Running the registry under the engine is LP-19's option A, the default the owner
  confirmed on 2026-11-07
  ([ADR-0012](../decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)); turning it
  on anywhere, the registry's signing key and DNS are the owner's.
- **Site reports** are served marked `verified: false` until sites sign them (LP-22); the Hub
  shows none.
- **The registry's signing key** has no rotation path; clients pin it at first contact, or trust
  it because it ships with them (the shipped file is empty until the owner makes the key).
- **`GET /v1/yanks` is not paged.**

The threat model's rows S18, S19, T7, T11, R3, D11, D12, E12 and E13 hold the registry's checks and
the tests that prove them ([threat model](../security/threat-model.md)).
