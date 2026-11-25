# Running a registry

A **registry** keeps signed agent archives with immutable versions, and anyone can pull and
verify them. It shows only what it checked
([ADR-0012](../decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)): it renders
each version's Decision Card itself, never shows the card, benchmark or leaderboard a publisher
uploads, and answers what it verified (`checked`) apart from what the publisher says
(`claimed`). This page is for whoever runs a registry, and for a deployment that serves agents
pulled from one.

There is no public registry yet. A registry of its own has no accounts and no shared secret
(LP-19g): a publisher writes with an owned token ([Publisher tokens](#publisher-tokens-that-belong-to-someone)),
and the operator's commands on its database do the rest: claims, publisher types, reviews,
the registry's recalls and deletions ([What the operator does](#what-the-operator-does)). A
registry can also run under the engine, where people sign in to claim and mint and the
registry's reviewers decide ([Under the engine](#under-the-engine)). The docker sandbox runs
one on port 8100.

## Starting a registry

```bash
export MENDARI_REGISTRY_SIGNING_KEY=$(openssl rand -base64 32)
mendari registry serve --db var/registry.db --port 8100     # a SQLite path or a postgresql:// URL
mendari registry verify --db var/registry.db                # read-only; exit 1 when broken
```

| Setting | Default | What it does |
|---|---|---|
| `MENDARI_REGISTRY_SIGNING_KEY` | unset: the registry recalls nothing itself and counter-signs nothing | 32 random bytes in base64, the Ed25519 key that signs the registry's own recalls (a reviewer's or the operator's) and counter-signs each version the registry lists ([below](#the-counter-signature)), published at `GET /v1/registry-key`. It is read when it is used, not at startup: after setting it, check that `GET /v1/registry-key` answers `200` with its id. |
| `MENDARI_REGISTRY_RATE_LIMITS` | `on` | `on`, `log` (count, never refuse) or `off`, then overrides such as `registry_write=240/h:40`. The defaults: reads 600 a minute per address (burst 200), writes 120 an hour per namespace (burst 30; all of a namespace's tokens share it), site reports 60 an hour per namespace (burst 10), site ticket requests and withdrawals 60 an hour per address (burst 10) before the signature is read and 20 an hour per site key (burst 5) after. A token the registry did not issue counts against its sender's address. A bad value stops the registry. |
| `MENDARI_REGISTRY_REGRESSION` | `on` | `off` publishes without the regression check. |

`MENDARI_REGISTRY_ADMIN_TOKEN` is no longer read (LP-19g). Set, the registry logs once at
startup that it is unused and names the commands that replace it; unset it
([From a registry with an administrator token](#from-a-registry-with-an-administrator-token)).

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
- **No shared secret.** No registry has an administrator token (LP-19g). Claims are approved
  and refused, and versions reviewed, recalled by the registry and deleted, by the registry's
  reviewers once they are set up ([Registry reviewers](#registry-reviewers)), and with the
  operator's commands on the database where they are not
  ([What the operator does](#what-the-operator-does)).
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
- **In the dashboard** the same administrators have two more tabs in the Access view:
  **Namespaces** (claim one, see whether the registry lists it, add a publisher key) and
  **Publisher tokens** (mint one, shown once in a dialog and never again; revoke, with a
  confirmation; who made each, when it expires and the last day it was used). The tabs are
  there only where the registry is on; a reserved or taken name is refused in words.
- **A request body is the engine's limit at most** (`MENDARI_MAX_BODY_BYTES`, 4 MiB by default;
  a host may allow less): a larger archive is a `413` that says so. A registry of its own takes
  100 MB. Larger archives under the engine wait for the agent store (LP-20).
- **Limits and logs.** The registry's own rate limits apply (`MENDARI_REGISTRY_RATE_LIMITS`,
  above), counted in the engine's database. Requests under `/registry/` get an access line in
  the engine's security log with the route's template (`/registry/v1/agents/{namespace}/{name}`),
  never the raw path or a token.
- **Two chains, one database.** The registry's events stay in its own chain (`hub_events`,
  `mendari registry verify --db`), the engine's in the audit chain; neither is written twice.
  The registry's events are not in the Access view's Activity tab.
- The engine does not close the function early for a long publish: the regression check runs
  within the host's request time limit.

## The hosted shape

A hosted registry is the engine's own deployment with `MENDARI_REGISTRY=on`: no project,
database or function setting of its own. On Vercel the engine's function already allows 300 s a
request and the regression check stops at 180 s. The Hub, the public site that reads the
registry, is a second project built from `hub/`, whose `REGISTRY_URL` is the engine's public
origin followed by `/registry`. The order to turn it on, what to watch and how to turn it off
are in the Vercel guide's
[Registry and Hub](https://github.com/mendari-hq/mendari/blob/main/deploy/vercel/README.md#registry-and-hub);
the on-premises and Kubernetes templates carry the same settings, commented out. A status
check's registry base is then the engine's origin with `/registry`
([status checks](status-checks.md)).

What running it adds to the engine's runbook:

- **Backup is the engine's.** The registry's tables (`hub_`, archives included) are in the
  engine's database, so its backups and restores cover them. After a restore, check both chains:

  ```bash
  mendari audit verify --db "$DATABASE_URL"
  mendari registry verify --db "$DATABASE_URL" --checkpoint /elsewhere/registry-checkpoint.json
  ```

  Each checks its own chain only: a restore that brought one back and not the other passes one
  and fails the other.
- **The chain's head is kept elsewhere**, every day, as [the chain](#the-chain) says: the
  database's credentials can rewrite both chains, and only a head kept where they cannot write
  shows it.
- **Turning it off** (unset `MENDARI_REGISTRY`, redeploy) deletes nothing: `/registry/` answers
  as it did before, the `hub_` rows stay, and turning it on again serves them as they were.
  Unsetting `MENDARI_REGISTRY_REVIEW_ORG` alone takes reviewing back to the operator's commands.

## What the operator does

A registry has no administrator over HTTP (LP-19g): over its routes only a namespace's own
publisher writes. Everything else is a command on the registry's database, which works while
the registry runs. Under the engine the registry's reviewers do the same through the web
([Registry reviewers](#registry-reviewers)). Each write is a chained event naming who ran the
command and, where the command asks for one, why ([The chain](#the-chain)).

| Action | Command |
|---|---|
| Claim a namespace for an organization (a reserved one too) | `mendari registry claim --namespace N --org ORG --reason …` |
| Approve or refuse a claim, after its first publish | `mendari registry approve` or `refuse --namespace N --reason …` |
| Set its publisher type, after checking who publishes there | `mendari registry publisher-type --namespace N --type verified --reason …` |
| Mint, list or revoke a publisher token | `mendari registry token`, `tokens`, `revoke-token` ([below](#publisher-tokens-that-belong-to-someone)) |
| Review a version | `mendari registry review N/NAME@VERSION --risk-level 0-4 --reason …` (one printable line, at most 300 characters, never case text) |
| Recall a version, signed by the registry | `mendari registry recall N/NAME@VERSION --severity high --reason …` (needs `MENDARI_REGISTRY_SIGNING_KEY`) |
| Delete a version that must not be served | `mendari registry delete N/NAME@VERSION --reason …` |
| Register, verify, key, revoke, close or list sites | `mendari registry site`, `site-verify`, `site-key`, `site-revoke-key`, `site-close`, `sites` ([below](#sites)) |

Every command takes `--db` (a SQLite path or a `postgresql://` URL). `scripts/hub_seed.py`
does the first steps for the bundled agents, today the demo agent (claim `mendari`, mint,
publish, approve).

A publisher, with its token, registers its keys (`mendari keys register`), publishes
(`mendari push`) and recalls its own versions (`mendari yank`, signed with one of its keys). A publish
that `mendari validate` would refuse is refused (`422`, naming up to five problems by file and
place, never a case; nothing is stored): the registry runs the same validation function as the
command line. A
publish whose archive holds cases `mendari feedback export` wrote is refused (`422`, naming
the files with how many times each is marked, never a case): they are a site's cases, which
`mendari push` refuses to send, and the registry keeps nothing of one sent by a client that did
not look. A review shows
in `checked.review` and `claimed.risk.confirmed`; the newest review of a version is the one
shown. A review's note stays in the chain for good.

## Recalls and deletions

A recall (`mendari.yank/1`) names one version exactly (agent, version, digest, archive), with a
reason, a severity, who signed it and when. A version is recalled once. A recalled version
leaves the latest version, search and the namespace's list; it is still served by its exact
version, with the record, and its archive stays. `GET /v1/yanks` lists every recall.

A deletion is for a version that must not be served at all: its archive answers `410` with the
reason, its bytes go once no version that is not deleted needs them, and its row and a
tombstone stay, so the version is never published again. `mendari pull` refuses a deleted
version and removes a cached copy.

### When an agent must stop

1. **Recall it.** The publisher runs
   `mendari yank ns/name@1.2.0 --reason "misroutes refills" --severity high --key keys/publisher.key`,
   or the registry recalls it, signed with its own key: a reviewer under the engine, or the
   operator's `mendari registry recall ns/name@1.2.0 --severity high --reason …`.
2. **Servers stop automating it, from their next read.** A server reads `GET /v1/yanks` of a
   registry its served agents came from in a thread of its own. A decision for one of that
   registry's agents starts the read, once the last one is older than
   `MENDARI_REGISTRY_RECALL_CHECK_S` (300 s unless set); that decision, and any made while the
   read runs, use the list read before. From then on a recalled agent no longer automates, and
   its own rule's BLOCK goes to a person, whether or not its organization approved it, and each
   held decision says why in its reasons. A server that decides all day has the recall within
   about five minutes; on a quiet one, the first decision after the recall can still automate.
   The server's full `/api/health` (signed in, or in open mode) shows, under `registry_recalls`,
   each registry's last read (`checked_s_ago`, which grows while no decision starts a read), how
   many versions it recalls (`recalled`), the error type while reads fail (`failing`) and
   whether it publishes a signing key (`signs`; see the known limits below). The
   Decision Card shows a recall the last pull recorded; one read since shows in health and in
   the decisions' reasons.
3. **Not soon enough, or a quiet server?** Hold the agent on each deployment now
   ([Holding automation](automation-hold.md)): a hold takes effect within seconds, with no read.
4. **Afterwards**, remove the recalled version from each server's agent cache
   (`agents/<namespace>/<name>/<version>` under `MENDARI_CACHE_DIR`, `~/.cache/mendari` unless
   set; a version pulled before 2026-11 is in the folder the cache had then,
   `packs/<namespace>/<name>/<version>`, where it is still read), or pull a fixed version.
   A new version has a new digest, so it waits for its organization's approval.
5. **If it must never be served,** delete it as well.

A recall a server cannot verify is still honoured: it only lowers automation. A serverless
instance starts unread and holds its pulled agents until its first read. `mendari decide` and
`Decision.from_hub` know only the recall the last pull recorded.

## Reaching a registry

The client (`mendari login`, `pull`, `push`, `search`, `yank`, `Decision.from_hub`) reaches a
registry over https only, and refuses any other address before it sends anything, so a
publisher token never crosses a network in clear. Plain http is allowed to this machine alone:
`localhost`, a `.localhost` name or a loopback address, which is what a local registry and the
docker sandbox use. An address with a user or a password in it is refused. Put a registry that
other machines use behind TLS.

## Serving pulled agents

An agent pulled from a registry never automates until your organization approves its exact
digest, at every risk level (2 and 3 need their governance approval too, and 4 never
automates); until then its own rule's BLOCK goes to a person. Approve it on its Decision Card
(the Automation approval section: where it came from, the digest served, any recall, then a
dialog naming the digest) or with `POST /api/registry-approvals` (`pack_id`, `pack_digest`, a
`reference` such as the committee's minutes; the two keys name the agent and keep the word
Mendari used before, as stored records hold them); `DELETE /api/registry-approvals/{id}` revokes
it at once. Both need the `approve` permission and are chained. An approval names one digest and
lasts until revoked. It never lifts a recall, a hold or a risk level's cap.

| Setting | Default | What it does |
|---|---|---|
| `MENDARI_REGISTRY_RECALL_CHECK_S` | 300 (1 to 604800 seconds) | How old a server's last read of a registry's recalls may be before a decision for one of its agents starts another (that decision uses the last list) |
| `MENDARI_REGISTRY_RECALL_STALE_S` | 86400 (at least twice the check) | After this long without a successful read, every agent from that registry is held |

These reads go only to the registry an agent's verified marker names, over https (plain http to
this machine), with no redirects, 5 s per step, 20 s for the whole answer and 5 MB at most.
They are not under `MENDARI_PROVIDER_ALLOWED_ORIGINS`. An agent pulled before LP-18d names no
registry and is held until it is pulled again.

A pulled agent is shown with the registry's card. Its uploaded benchmark is never a reference
here: the Monitoring view says it is not compared (`benchmark.reason: from_registry`) and runs
only the checks that need no benchmark, and an evaluation says it is not compared with the
benchmark the agent carries. Give `mendari eval --baseline report.json` a report of your own to
compare with.

## The regression check

On every publish the registry evaluates the version on its own test set
(`evaluation.benchmark_dataset`), in a child process with no credential, local providers only
and 180 s at most, and compares it with the newest earlier version's check (the agent gate). It
shows the result as `checked.regression`, labelled "publisher's own test set: regression check,
not a ranking", and never refuses a version for it. The publisher chose the set, so it can show
a regression, never rank. An agent whose default providers call a model is not measured
(`needs_remote`). At most two checks run at once; a publish past that is told to try again
(`503`).

## Publisher evaluations

A publisher publishes its evaluations of a listed version, one per configuration and public
dataset ([ADR-0020](../decisions/ADR-0020-publisher-evaluations.md)): `mendari push AGENT --key
publisher.key --evaluate NAME` publishes the version and every configuration's evaluation in
one step, and `mendari eval AGENT --dataset NAME --publish --key publisher.key` one
configuration ([Publishing an agent's evaluations](../guides/publishing-evaluations.md)). Each
sends a `mendari.publisher-evaluation/1` record
([the format](../spec/evaluation-reports.md#publisher-evaluations-mendaripublisher-evaluation2);
a `/1` record from an older client is still taken, and shown with what it does not state as
not stated)
to `POST /v1/agents/{namespace}/{name}/{version}/evaluations` with the namespace's publisher
token, which is checked before the body is read (at most 256 KB; writes count against the
namespace's `registry_write` rate). Then, in order, each refusal naming the field and never a
value:

1. the record's shape (closed), its time (within 10 minutes of the registry's clock), and that
   it names this route's agent and version;
2. the key: one of the namespace's, not revoked, and the signature over the record's own
   prefix and format (no other Mendari signature is over those bytes);
3. the version: listed (not recalled, deleted, or in a namespace whose claim is not approved),
   the evaluation's digest the version's; every slot with a variant the version declares, and
   the counts' names the agent's;
4. the dataset: one the manifest declares, in the signed archive, with the SHA-256 and n given,
   and, in a `/2` record, the provenance the manifest states (`card.provenance`) and no other;
   the counts whole (no null cell: a public set is not suppressed), adding up, under the
   agent's own policy, and `remote_consent` exactly whether the configuration calls a model;
5. a configuration whose every provider is local is run again in the regression check's
   isolated child (`hub/regression.py` `rerun`: same limits, at most two at once, `503` past
   that) and refused unless its counts are the publisher's exactly. With the check off
   (`MENDARI_REGISTRY_REGRESSION=off`) such a configuration is refused, never taken unchecked.
   One that calls a model (`agent`, `llm`; an `agent` variant that answers offline from
   its `mock_file` too, as the registry goes by the provider's type) is not run again.

One evaluation is live per version, configuration and dataset. A later one supersedes it:
chained `evaluation.published` (with the evaluation's id, the SHA-256 of the signed record, its
configuration, dataset, key and whether it was re-run) and `evaluation.superseded`; both rows
stay. The same signed record sent again is answered as it was while it is live and refused
once superseded; one signed before the live evaluation is refused.

`GET /v1/agents/{namespace}/{name}/{version}/evaluations` (`mendari.publisher-evaluations/1`)
lists the live ones, ordered by configuration, never by a figure: each with its configuration,
dataset (name, n, SHA-256), key, the models and latency and cost the publisher reported, whether
the registry re-ran it, and the figures the registry works out from the counts, each k of n with
its exact interval: each critical label's detection and automated misses, the primary question's
accuracy, false automation, and the outcomes. The Agent Hub shows them as "Published
evaluations", apart from what the registry checked and from site evidence.

**Every stored record is checked again on every read.** What was checked at publication is a
row in a database afterwards, so each listing verifies each record as it serves it: the body is
still the closed record of its format, its id, agent, version, configuration, dataset, key and
time are its row's, and its signature holds with a key of the namespace's. One that fails is
listed as not verified, with no figure, model or claim of its own, and takes part in nothing
below.

**The evaluation page and the badge.** Three more reads, each public and counted against the
read limit. They are newer than the routes' renaming, so each answers at this one path only:

- `GET /v1/agents/{namespace}/{name}/evaluation` (`mendari.agent-evaluation/1`,
  [the format](../spec/evaluation-reports.md#an-agents-evaluation-in-public-mendariagent-evaluation1)),
  of the agent's newest version that is not recalled, or `?version=`: its `state` (`evaluated`,
  `none`, `not_verified`, `recalled`), each dataset with its configurations and their figures,
  the `comparison`, and `not_shown`: each stored evaluation that does not verify or whose key
  was revoked since, by its row's names. The Agent Hub's page
  `/agents/<namespace>/<name>/evaluation` is built from it and decides nothing itself.
- `GET /v1/agents/{namespace}/{name}/badge.svg`: the same in one line, for a README. Its words
  are the registry's and its numbers whole counts: no name anyone chose, no rate, never green.
  It holds no script, style, link or reference; it alone is served as `image/svg+xml`, may be
  kept for five minutes and shown by another site's page, and sets no cookie. An agent that is
  not there is a `404` with a badge that says so.
- `GET /v1/agents/{namespace}/{name}/{version}/evaluations/{id}`: one signed record as stored,
  for anyone to check against the namespace's key.

**Which configuration did best.** The comparison lists a dataset's configurations in the order
of `mendari.rank/1`, without its cost. That order always lists one first; the registry says a
configuration did best (`comparison.best`) only when it is alone in the first band, that is,
when every other configuration is behind it by more than their intervals explain, and names
none otherwise (several in `comparison.first_band`, a tie, one configuration, or no critical
label with 100 positive cases). ADR-0020, as amended on 2026-11-24, says why.

**Naming the Hub.** Set `MENDARI_REGISTRY_HUB_URL` on the registry to the Agent Hub's public
address (`https://hub.example`; https, or http on this machine, with no user, password, query
or fragment). The evaluation's answer then carries `links.page`, and `mendari push --evaluate`
prints it with the badge's Markdown. Unset, the command prints the badge linked to the
registry's own answer and says the registry names no Hub. A value that is not such an address
stops the registry at start-up, naming the setting and never the value. The Hub itself reads
`REGISTRY_URL`, and `REGISTRY_PUBLIC_URL` where it reaches the registry at an address the
public cannot ([`hub/README.md`](https://github.com/mendari-hq/mendari/blob/main/hub/README.md)).

A publisher key that is lost is revoked by the operator: `mendari registry revoke-key NAMESPACE
KEY_ID --reason "..." --db …` (chained `key.revoked`). It signs no archive, recall or evaluation
after; the versions it signed stay, and its evaluations stay listed with `excluded: "key
revoked"`, out of the comparison. `GET /v1/namespaces/{namespace}` lists each key with
`revoked_at` (null while it is live), and the Agent Hub marks a revoked key on the publisher's
page. `mendari pull` follows the registry: a revoked key still verifies a version the registry
received before it was revoked (the version's `published_at`) and a recall signed before then;
anything it signed later, or with no time, is refused (a recall stays a recall, unverified).

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
- A token from before owned tokens (one an administrator issued over HTTP until LP-19g) has
  no owner or expiry and cannot be revoked, so it no longer writes: the registry answers it as
  any unknown token (`403`), and it reads what the public reads. Its row and its chained event
  stay, so `mendari registry verify` still passes. Mint an owned token in its place.

### From a registry with an administrator token

LP-19g removed the shared administrator token and its routes. On a registry that had them:

1. Unset `MENDARI_REGISTRY_ADMIN_TOKEN` (set, it is only logged as unused).
2. For each namespace a token from before publishes to, mint an owned one
   (`mendari registry token --namespace N --org ORG --reason …`) and hand it to the publisher:
   the old one is refused from the upgrade on. `mendari registry tokens` lists what was minted.
3. Replace each call to a route that is gone with its command
   ([What the operator does](#what-the-operator-does)): `POST /v1/namespaces` with `claim`
   (and `approve` after the first publish), `PATCH /v1/namespaces/{namespace}` with
   `publisher-type`, `POST /v1/namespaces/{namespace}/tokens` with `token`, `POST …/review`
   with `review`, an administrator's `POST …/yank` with `recall`, and `POST …/delete` with
   `delete`. Each of those routes now answers `404` or `405`. A publisher's own signed recall
   (`mendari yank`) is unchanged.

Namespaces made before stay listed (they have no claim), and nothing in the database changes.

## Namespace claims

A claim links a namespace to an organization. It is `pending` until it is approved or refused,
once, and until it is approved nothing of the namespace is served to anyone but its own token
and a reviewer: to everyone else it answers as a namespace that does not exist (the same `404`),
in every read, search, the recall list and health's agent count. A namespace with no claim (one
from before claims, which an administrator made over HTTP until LP-19g) is listed as before.

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
- **Approved**, the namespace's later versions and new agent names are listed at once. A
  decision is final: an approved claim is not refused later (recall or delete a version
  instead), and a refused one is never approved.
- **Refused**, nothing is removed: its versions and archives stay in the database, served only
  to its own token and a reviewer, and its token reads the note.
- Each step is chained with the organization, who ran the command and why (`namespace.created`
  and `namespace.claimed`, then `namespace.approved` or `namespace.refused`), and every
  `agent.published` records the organization and whether the version was listed then.
- Over the registry's own routes nobody reads as a reviewer: reviewers read through the
  engine's routes, and the operator with the commands (`mendari registry claims`).

## Sites

A site is a health system that evaluates agents on its own chart-reviewed data and shares signed
counts (LP-22, [ADR-0017](../decisions/ADR-0017-a-site-network-of-signed-counts.md)). It is a
record owned by an organization, `pending` until it is verified, once, by a registry reviewer or
by the operator's command with how it was checked; `refused` or `closed` sites stay stored and
are never served. A site signs with its own Ed25519 keys: at most 5 live at once, never a
namespace's publisher key nor another site's (one key never signs in two roles). A key is added
only with its holder's proof, the key's own signature over the site's id and the key's id
(`mendari site init` makes it), so nobody can register a key they saw but do not hold; and only
the key is stored, never the text it came in. Rotating is
adding a key and revoking the old one. A revocation says whether the key is thought
compromised: if it is, every report it signed leaves the pooled figures and stays listed as
such; if not, reports received before it stand. Every write is chained with the organization
(`site.registered`, `site.verified`, `site.refused`, `site.closed`, `site.key_added`,
`site.key_revoked`).

```bash
mendari registry site --db var/registry.db --org north-health --name "North Health" \
  --reason "asked by its CMIO"
# the site runs `mendari site init` and sends the key file it writes; then:
mendari registry site-key --db var/registry.db --site ID --key site-key.json --reason "first key"
mendari registry site-verify --db var/registry.db --site ID --reason "called the CMIO back"
mendari registry site-revoke-key --db var/registry.db ed25519:… --reason "rotated"
mendari registry site-revoke-key --db var/registry.db ed25519:… --compromised --reason "laptop lost"
mendari registry site-close --db var/registry.db --site ID --reason "merged into another"
mendari registry sites --db var/registry.db [--org ORG] [--status pending]   # read-only
```

`GET /v1/sites/{id}` serves a verified site to anyone: its name, its status, when it was
verified, and its keys' ids with their dates and whether each was revoked as compromised (a
reader can then tell which signed reports leave the pool). A pending, refused or closed site answers as one
that does not exist. Check who an organization is outside Mendari before verifying its site:
a site's name is as good as that check. A site whose organization holds an agent's namespace (its
claim) is the publisher's own site for that agent: its reports are listed and left out of the
pool.

### Tickets

Before a site evaluates an agent it asks for a ticket (`mendari site ticket`, LP-22e): a
`mendari.site-ticket-request/1` signed with one of its live keys, bound to the agent's digest,
the configuration (each slot's variant), the dataset's size and a keyed commitment to the
dataset (an HMAC under a secret that never leaves the site; the dataset's own hash is never
sent). `POST /v1/tickets` reads at most 8 KB and checks, in order: the key is a live key of a
verified site; the signature; the clock (10 minutes either way); the nonce is new for the site;
the version is published, listed, not recalled or deleted, and its digest is the one sent; the
configuration names a variant the version declares for each of its slots; a configuration that
is not wholly local carries the site's consent; at least 100 cases; and no live ticket or report
holds the same site, digest, configuration and commitment. The answer holds the ticket and a
one-time submission secret (stored as its SHA-256), shown once. A ticket lasts 30 days; that
it expired is worked out on read, never written. A site gives up an open ticket with a signed
withdrawal (`POST /v1/tickets/{id}/withdraw`); it stays listed, and the same dataset gets no
second ticket for that digest and configuration. An expired ticket is not withdrawn: it stays
unreported. Chained `site.ticket_issued`
and `site.ticket_withdrawn`.

`GET /v1/agents/{namespace}/{name}/tickets` lists, per verified site, digest and configuration,
how many tickets were reported, are unreported (open, or expired with no report) and were
withdrawn, with the first and last dates: a ticket with no result stays counted for good, a
closed site's too. No
commitment and no secret is served.

On the site's machine (`mendari site`): `init --site ID` makes its key and commitment secret
in the client's configuration directory (mode 0600) and writes the key file the registry takes;
`key --new` makes a new key; `ticket AGENT --dataset FILE` asks for a ticket (the deterministic
arm unless `--provider` names variants; a remote one needs `--consent-remote`), keeping the
ticket and its secret there too, or with `--out FILE` writes the signed request to carry to a
machine that reaches the registry.

### Signed reports

A verified site answers one ticket with a `mendari.site-submission/1` it signed (`mendari eval
--share`, or `--share-out` then `mendari site submit`), sent to
`POST /v1/agents/{namespace}/{name}/{version}/reports` with the ticket's one-time secret as the
bearer credential: the registry finds the ticket by the secret's SHA-256 before it reads the
body, then reads at most 2 MB and checks that the ticket is the record's and still open (not
expired, withdrawn or reported), that the key is a live key of the ticket's site, the signature
over the exact fields, the nonce, that the report's agent, digest, configuration, size and
consent are the ticket's, its names, and that suppression changes nothing. The report and the
ticket's `reported` status are written in one transaction, chained `report.shared` (with the
site, key, ticket and the body's SHA-256) and `site.ticket_reported`. A publisher token no
longer shares a report (401, naming `mendari site init`).

`GET /v1/agents/{namespace}/{name}/reports` lists the signed reports (`verified: true`, each
with its site, key, ticket, whether a correction superseded it and whether its key was revoked
as compromised, and its figures worked out from its counts) and how many are superseded;
`GET …/reports/{id}` serves the signed record, to check against the site's key. A correction
asks for a ticket naming the report it supersedes (`supersedes`); both stay listed. Unsigned
reports stored before LP-22f stay in `hub_reports` and are no longer served.

A submission carried by hand is taken by the operator through the same checks (but the
secret): `mendari registry accept-report --db … submission.json`.

`GET /v1/agents/{namespace}/{name}/site-evidence` (`mendari.site-evidence/1`, LP-22g) puts the
signed reports side by side per digest and configuration: each site's k of n first, then the
pooled figures, the homogeneity test and its warning, with the publisher's own site, reports
under a compromised key and superseded reports left out and listed, and the unreported and
withdrawn tickets counted beside them
([the format](../spec/evaluation-reports.md#site-evidence-mendarisite-evidence1)).

### Sites under the engine

In memberships mode, an organization's administrators (the `keys` permission, people only)
register its evaluating sites on the dashboard's Sites tab (`/api/registry/sites`), add a site's
key from the key file `mendari site init` wrote on the site's machine (its public key and proof;
the private key is never typed into the dashboard) and revoke a key with why and whether it is
thought compromised. Another organization's sites answer as ones that do not exist. The
registry's reviewers see the sites waiting on the review screen
(`/api/registry/review/sites`) and verify or refuse each, once, with a note; a reviewer never
decides a site they registered or their own organization's. Each act is chained with the
person's id and logged (`admin.registry_site.registered`, `admin.registry_site_key.added`,
`admin.registry_site_key.revoked`, `admin.registry_site.verified`,
`admin.registry_site.refused`). An organization holds 20 sites at most.

## Registry reviewers

Under the engine, in memberships mode, the people who decide claims are members of one
organization: the one `MENDARI_REGISTRY_REVIEW_ORG` names. Unset (the default), nobody reviews
through the web and the operator's commands do. To set it up:

```bash
mendari access org create registry-review --name "Registry review" --db "$DATABASE_URL"
# set MENDARI_REGISTRY_REVIEW_ORG=registry-review and restart; then an administrator of that
# organization gives a member the role `registry_reviewer` (the Access view, or
# PUT /api/auth/members/{id}/roles).
```

- **One organization only.** Any administrator gives any membership role, so a reviewer role
  that counted everywhere would let every organization approve its own namespace.
  `registry_reviewer` grants `registry_review` only for a membership of the review
  organization. Giving it in any other organization is refused (`422`), and a membership that
  holds it elsewhere anyway gains nothing. The review organization's own administrators are
  not reviewers until they hold the role; whoever administers that organization decides who
  reviews, so keep it small and its administrators few.
- **People only**: never an API key, a Backend Services client, a publisher token, open mode
  or claims mode (where roles come from the identity provider, and this one is never read
  from it). Another site's page cannot call these routes (the engine's origin check).
- **It never stops the server.** The setting must name an organization that has a record
  (`mendari access org create`), with `MENDARI_REGISTRY=on` and memberships mode. Otherwise
  the server starts, logs why the setting is not used, mounts none of these routes and treats
  the role as granting nothing. An id no organization could have stops the server, like any
  malformed setting.

In the dashboard, a reviewer has the **Registry review** screen (`#registry-review`): each
pending claim with its organization and who claimed it, and each of its newest versions with
what the registry checked beside what the publisher claims, and the card the registry rendered
(read as plain text when opened). Approve and Refuse ask for a one-line note, which the claim's
organization sees; a claim you filed shows without either. Someone who holds nothing but the
reviewer role sees this screen alone. Through the API:

| Route | What it does |
|---|---|
| `GET /api/registry/review` | The pending claims, oldest first (50 at most): each with its organization, who claimed it (their id), its publisher keys by id, and its newest versions (5 at most) with what the registry `checked` apart from what the publisher `claimed`. No cards: a publisher chooses a card's size |
| `GET /api/registry/review/agents/{namespace}/{name}/{version}` | One version of any namespace, listed or not, with the card the registry rendered |
| `POST /api/registry/review/namespaces/{namespace}` `{decision, note}` | `approve` or `refuse` the claim, once |
| `POST /api/registry/review/namespaces/{namespace}/publisher-type` `{publisher_type}` | Record who publishes there: `community`, `verified` or `institutional` |
| `POST /api/registry/review/agents/{namespace}/{name}/{version}` `{risk_level, note}` | Record the risk level a version's review found |
| `POST /api/registry/review/agents/{namespace}/{name}/{version}/recall` `{reason, severity}` | Recall it, signed by the registry (`409` without `MENDARI_REGISTRY_SIGNING_KEY`) |
| `POST /api/registry/review/agents/{namespace}/{name}/{version}/delete` `{reason}` | Delete its archive for good, keeping a tombstone |

- **A reviewer never acts on a namespace they claimed themselves** (`403`): not its decision,
  its publisher type, a review, a recall or a deletion. The queue marks it `own`, and another
  reviewer acts. A reviewer may still act on a namespace a colleague of their other
  organization claimed; who reviews what beyond that is the review organization's to arrange.
- **Changing the review organization changes who reviews, at the restart.** A membership of
  the newly named organization that already holds the role (the operator's `mendari access`
  commands give any role anywhere) reviews from then, with no new event, and the old
  organization's reviewers stop. The server logs how many members hold the role each time it
  starts: read that line after changing the setting.
- A reviewer's writes are counted per person (60 a minute), and setting a publisher type to
  what it already is chains nothing.
- Each act is chained in the registry's chain by the store method the operator's command uses,
  with the reviewer's id (`user:<hash>`, never an address) as `by` (a review's
  `reviewer`), and a claim's decision with the claim's organization. A review's `reviewer` is
  served with the version, as before: an id, not a name. The security log gets
  `admin.registry_claim.approved|refused`, `admin.registry_namespace.publisher_type_set` and
  `admin.registry_agent.reviewed|recalled|deleted`, with ids only.
- Notes and reasons are one printable line, 300 characters at most, kept for good: about the
  agent or the publisher, never case text.

Where nobody signs in, or there is no review organization, the operator does the same on the
database, each chained with who ran the command:

```bash
mendari registry publisher-type --db var/registry.db --namespace acme --type verified --reason "met them"
mendari registry review acme/intake-agent@1.2.0 --db var/registry.db --risk-level 1 --reason "reads as level 1"
mendari registry recall acme/intake-agent@1.2.0 --db var/registry.db --severity high --reason "a fault"   # needs the signing key
mendari registry delete acme/intake-agent@1.2.0 --db var/registry.db --reason "carried what it should not"
```

## The counter-signature

With `MENDARI_REGISTRY_SIGNING_KEY` set, the registry signs a record for each version when it
becomes listed (`mendari.registry-acceptance/1`: the agent, version, digest, archive SHA-256, the
publisher key that signed the archive, the registry's key id and the time). It does so at
publish in a listed namespace, and at approval for every version published while the claim was
pending. A version of a refused namespace is never counter-signed, nor a deleted one. The record
is served with the version as `checked.accepted` (`null` when there is none), and the Hub shows
"Counter-signed by the registry" or "Not counter-signed".

A version written straight into the database has no record. `mendari pull` checks the record
before it fetches the archive:

| The registry | The version's record | The pull |
|---|---|---|
| publishes a key this machine trusts | verifies, and names exactly this agent, version, digest, archive and publisher key | pulled; the marker beside the agent records `accepted: true` |
| publishes a key this machine trusts | missing, does not verify, or names something else | **refused**, nothing cached |
| publishes no key, and never did to this machine | (none) | pulled as before; `accepted: null`, and `mendari pull` says it is not counter-signed |
| published a key before, and now publishes none or another | (any) | **refused**, as a changed host key is |

A machine trusts the registry's key when it is one shipped with the client
(`src/mendari/hub/registry_keys.json`, public keys only, never a hostname) or the one it pinned
for that registry's URL on first use (`trusted_keys.json` in `~/.config/mendari`, the
`@registry` entry). The shipped file holds the Mendari registry's key (`ed25519:85f132e2be678b2c`,
made 2026-11-15); a self-hosted registry's key is pinned on first use
(`MENDARI_REGISTRY_COUNTERSIGNATURE`, below, decides what else a machine accepts).

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
  verified, and a chain rewritten whole would have too. Run with one, it prints
  `"events_since_checkpoint"`: how many events the chain held after the checkpoint's head.
  The checkpoint says nothing about those, so when the number is not 0 the command says so on
  standard error too. A backfill against an old checkpoint is only as checked as the events
  since are read: take a fresh checkpoint (with `--previous`) just before, or read them;
- checks each version again from what is stored before it signs: its archive is there and
  hashes to its name, is signed by a key of its namespace, and its signed manifest names this
  agent, version and digest. One that fails is skipped and printed with why, and the command
  exits 1. An approval makes the same check on the versions published while the claim was
  pending;
- prints the versions it signed, chains each record (`agent.accepted`) with who ran it, and signs
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
  asks each registry its agents came from whether it publishes a signing key, in the same read
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
(`mendari.checkpoint/1`) signed by the same key is never read as one.
The command opens the database read-only and stores nothing in it (a copy there could be
rewritten with the chain); it never makes a missing database. It refuses to sign when the chain
does not verify and, with `--previous`, when the chain no longer holds the last statement's
head, so a rewrite stops the routine instead of being signed over. The first run has no
`--previous`. Keep the files where no credential of the registry can write: another account's
bucket with versioning, a repository, a mailbox. Keep the dated copies too if you want to know
since when.

`mendari registry verify --checkpoint FILE` checks the statement's signature with the
registry's key, then the chain, then that the head the statement names is an event of the
chain. It does not need the private key: on a host that does not hold
`MENDARI_REGISTRY_SIGNING_KEY` (an auditor's machine, a monitoring job), give the public key
instead, as `GET /v1/registry-key` shows it:

```bash
mendari registry verify --db var/registry.db --checkpoint /elsewhere/registry-checkpoint.json \
  --public-key "$(curl -s https://registry.example/v1/registry-key | jq -r .public_key)"
```

Better still, keep the public key with the statements, so the check does not trust the
registry for its own key. `checkpoint` and `countersign` sign, so they need the private key. A chain rewritten or cut short from
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

- **Accounts (LP-19).** Under the engine, an organization's administrators claim namespaces
  and make publisher tokens ([Under the engine](#under-the-engine)); and the
  members of one review organization decide claims ([Registry reviewers](#registry-reviewers)),
  in the dashboard as through the API. A registry of its own has no accounts: its operator
  mints tokens and decides with commands, and no registry has a shared administrator token
  (LP-19g). Running the registry under the engine is LP-19's option A, the default the owner
  confirmed on 2026-11-07
  ([ADR-0012](../decisions/ADR-0012-a-registry-that-shows-only-what-it-checked.md)); its
  configuration is written ([The hosted shape](#the-hosted-shape)), and turning it on anywhere,
  the registry's signing key, the Hub's project and DNS are the owner's.
- **Site reports** are signed and checked on receipt (LP-22f, [Signed reports](#signed-reports));
  the Hub shows none until LP-22h.
- **The registry's signing key** has no rotation path; clients pin it at first contact, or trust
  it because it ships with them (the Mendari registry's key ships from 2026-11-15).
- **`GET /v1/yanks` is not paged.**

The threat model's rows S18, S19, T7, T11, R3, D11, D12, E12 and E13 hold the registry's checks and
the tests that prove them ([threat model](../security/threat-model.md)).
