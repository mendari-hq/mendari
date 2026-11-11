<!-- Planned on 2026-11-09 against main a88ad5b6, and its numbers re-checked on 78be63c9 (ADR-0015
and the threat-model rows below were still free). One reader mapped the registry
(src/mendari/hub), the engine's guard, accounts and audit store, the dashboard's Access view, hub/
(Next.js), vercel.json and deploy/, the gates and the threat model, read-only. Nothing was built
or run. Where the build prompt or the owner's note disagrees with the code, the conflict and the
default each PR follows are written below. tasks/todo.md tracks progress. -->

# LP-19 build plan: registry accounts and deployment (A10, part 2)

> **Numbers this plan takes** (free at 78be63c9; other lanes are open, so 19a re-checks them before
> it writes anything, and `tests/test_threat_model.py` checks the order): **ADR-0015**.
> Threat-model rows **S22, S23, T11, R6, I19, I20, D15, E17** (the last rows today are S21, T10,
> R5, I18, D14, E16).

## Summary

LP-19 ties publishing to a signed-in person and their organization, holds a new namespace's first
publish until a reviewer approves it, and runs the registry inside the engine app (the owner's
option A, 2026-11-07, `tasks/todo.md:2918-2951`). It builds on what LP-18 left
(`src/mendari/hub/registry.py`) and on LP-12's memberships (ADR-0008).

LP-19 delivers:
- **Owned publisher tokens.** A token has an owner (a person's id), an organization, a namespace,
  a creation time, an expiry (90 days unless asked, 365 at most, the engine keys' bounds,
  `core/audit.py:656-657`), a revocation, and a last-used day. People mint, list and revoke them
  in the dashboard's Access view. Today a token is three columns and can never be revoked
  (`registry.py:124-128`, `:379-400`).
- **Namespace claims.** An organization's administrator claims a namespace; `mendari` and
  `openhealth` are reserved. The namespace, its keys and its versions are invisible to search,
  pull, the namespace list, the recall list and the Hub until a registry reviewer approves the
  first publish. Today any namespace an administrator-token holder creates is listed at once
  (`registry.py:304-324`, `:1461-1466`).
- **Organization to namespace.** The claim is the link: one organization per namespace, kept in
  the registry's own tables, readable by LP-22.
- **Reviewers.** A registry reviewer is a person, not a shared secret: claims and first
  publishes, the risk review (`registry.py:1525-1544`), the publisher type, deletion and the
  registry's own recall all move from the admin token to them. The admin token is removed.
- **The registry under the engine.** `MENDARI_REGISTRY=on` mounts the registry's public routes at
  `/registry/v1/...` on the engine app, on the engine's database, with the engine's sign-in for
  people. Unset (the default), the engine is byte-for-byte as today.
- **Client trust.** The registry counter-signs each version it accepts with its own key
  (`MENDARI_REGISTRY_SIGNING_KEY`, `hub/yank.py:40`, `:75-89`); the client checks the
  counter-signature against a key shipped in the package or pinned; the client refuses plain
  http except to this machine (today `RegistryClient` takes any URL, `hub/client.py:108-120`).
- **Deployment configuration only**: the mount switch, the Hub as a second Vercel project
  (`hub/`), the status checks, and the docs. Nothing is deployed.

It is built in **11 PRs, 19a to 19k**.

Order of work:
- 19a first.
- **In parallel once 19a is merged:** 19b and 19h (https in the client; it touches only
  `hub/client.py`).
- 19c follows 19b (both rewrite `hub/registry.py`).
- **In parallel once 19c is merged:** 19d (the mount and the people's routes) and 19i (the
  counter-signature).
- 19e follows 19d. 19f needs 19d and 19e. 19j needs 19d and can be built beside 19e and 19f.
- 19g (the admin token removed) needs 19e, and merges after 19f so the dashboard never offers a
  path that is gone.
- 19k closes LP-19.

Where PRs collide, merge in this order:

| File | Merge order |
|---|---|
| `src/mendari/hub/registry.py` | 19b, 19c, 19i, 19d, 19e, 19g |
| `src/mendari/server/auth.py` | 19d, then 19e |
| `src/mendari/hub/client.py` | 19h, then 19i |
| `tests/conftest.py` (the `registry` fixture) | 19b, 19c, then 19g rewrites it |
| `tests/test_shipped.py` (`CHAINED`) | 19d, 19e, then 19g |
| `docs/operations/registry.md` | 19b, 19c, 19j, then 19g rewrites it |

These stay off until the owner acts:
- With `MENDARI_REGISTRY` unset, the engine mounts nothing and opens no `hub_` table.
- With `MENDARI_REGISTRY_REVIEW_ORG` unset, nobody is a reviewer through the web: a first publish
  stays invisible (fail closed) until the operator's command approves it.
- With `MENDARI_REGISTRY_SIGNING_KEY` unset, the registry counter-signs nothing and recalls
  nothing of its own (as today, `registry.py:1566-1570`).
- The shipped key file ships empty, like `scoreboard/keys.json` does today.
- Production gets none of these settings from a build PR.

What LP-19 does not change:
- What a valid pack decides, any pack's digest, any policy. No reference pack is rebuilt.
- The archive format, the signature check, the engine a pulled pack runs on, the cap on a pulled
  pack and the per-organization approval (`/api/registry-approvals`, `server/app.py:1811-1870`).
  A registry reviewer's approval lists a pack; it never lets one automate.
- The registry's unkeyed chain and `mendari registry verify` (`registry.py:821-902`): every new
  write is chained in `hub_events` with its row's hash, like the nine writes today.

Figures: LP-19 computes no statistic. The reviewer's queue and the Access view show counts of
rows (tokens, claims), never a rate. The regression check's accuracy stays as LP-18g serves it
(`registry.py:1079-1094`, k of n through `figures.counted`).

Every PR:
- passes the Definition of shipped (`notes/vision-audit.md:1310`);
- runs its tests on SQLite and on Postgres (the `db_target` fixture, `tests/conftest.py:276`),
  with warnings as errors;
- updates `README.md`, `CHANGELOG.md`, the LP-19 entry in `tasks/todo.md` and the docs it names;
- puts the SPDX line on new source files and signs off every commit (`git commit -s`);
- makes its test keys and tokens at runtime and commits none;
- writes no production hostname, project id or deployment setting in the tree
  (`tests/test_public_tree.py`).

## What the code is today (the facts the plan rests on)

1. **Tokens.** `hub_tokens (token_sha256, namespace, created_at)` (`registry.py:124-128`).
   `issue_token` makes `ohr_` + 32 random bytes (`:379-394`); `token_namespace` is the only check
   (`:396-400`). Only the admin token mints one (`:1475-1478`).
2. **The admin token** (`MENDARI_REGISTRY_ADMIN_TOKEN`, `registry.py:1361-1366`) creates
   namespaces (`:1461-1466`), sets the publisher type (`:1468-1473`), mints tokens, records
   reviews (`:1540`), deletes versions (`:1598`), signs the registry's recalls (`:1553-1582`),
   **and passes as a publisher of every namespace** (`require_publisher`, `:1431-1436`), which is
   the publishing path the build prompt forbids keeping. It also submits site reports (`:1679`).
3. **The chain covers rows by hashing every column** (`_row_sha`, `registry.py:234-236`;
   `ROWS`, `:213-232`), and `verify` refuses a row whose hash is not its newest event's
   (`:871-890`). So a new column on an existing table changes every old row's hash, and a column
   that changes without an event (a last-used time) breaks `verify`.
4. **Reads are public and unfiltered**: `latest` (`:915-938`), `versions`, `version`, the
   archive (`:1658-1669`), the namespace and its keys (`:1486-1490`), search (`:1704-1730`),
   the recall list (`:1610-1614`), and `health`'s pack count (`:1452-1459`).
5. **The engine's guard** resolves a permission per (method, path) from `_RULES`
   (`server/auth.py:240-339`); a route no rule lists needs `admin` (`:337` is the last `/api/`
   rule; `Auth.permission`, `:873-882`). A `public` route returns before any credential is read
   (`Auth.check`, `:1151-1153`). An `Authorization` header with a prefix other than `ohk_` or
   `mat_` is refused (`:1107`), so an `ohr_` token never passes as an engine credential.
6. **People and roles.** `keys` is a people-only permission the `admin` role holds
   (`auth.py:158-187`), and it already guards `/api/auth/keys` (`:300`). A membership may hold the
   reserved roles `pack_author` and `labeller` (`core/accounts.py:35-39`); only `labeller` grants
   anything, and only in memberships mode (`auth.py:1045`). Any organization's administrator may
   give its members any membership role.
7. **The engine's API keys have no last-used field** (`core/audit.py:362-376`). The owner's note
   expects one ("last used"); they have none. See conflict 3.
8. **One database, two stores.** `RegistryStore` opens its own connection through `core/db.py`
   with its own advisory lock (`registry.py:108`, `:276-288`; the audit store's is
   `core/audit.py:67`). Both schemas create the same `rate_limits` table
   (`core/rate_buckets.py:17-23`), so they can share a database.
9. **The engine caps every request body at 4 MiB** by default (`settings.py:111-122`, "under
   Vercel's 4.5 MB"; `BodyCap`, `server/app.py:2987`, `:3851-3892`). The registry accepts
   archives up to 100 MB (`hub/archive.py:52`). See conflict 5.
10. **`hub/registry.py` imports from `server/app.py`** (`registry.py:93`), so the engine can
    only import the registry inside `create_app`, never at module level.
11. **The function timeout is already 300 s** (`vercel.json:13-16`), above the regression
    check's 180 s (`hub/regression.py:37`). See conflict 6.
12. **The client** pins the registry's key on first use (`hub/client.py:375-398`) and namespace
    keys likewise (`:404-425`); nothing is shipped with it and no version is counter-signed. The
    server's recall reads already require https (`server/recalls.py:69-74`); the client does not.
13. **The Hub** reads `REGISTRY_URL` at request time, on the server (`hub/lib/registry.ts:4`),
    and its CI jobs start `mendari registry serve` and seed it with the admin token
    (`.github/workflows/ci.yml:312-361`, `:369-389`; `scripts/hub_seed.py:44-49`, `:109-112`).

## Pull requests, in order

- [ ] **19a** The plan, ADR-0015 (Proposed), and the conflicts written to `tasks/todo.md`.
  - **What it builds:**
    - `notes/lp19-plan.md` (this plan).
    - `docs/decisions/ADR-0015-registry-accounts-under-the-engine.md` (Proposed). It records:
      the mount and its switch; tokens owned by a person and an organization; the claim as the
      organization's link; one approval, at the first publish; reviewers as members of one named
      organization; the operator's commands as the only path without sign-in; the admin token
      removed; the counter-signature and the shipped key; https. It supersedes ADR-0012's
      default 5 ("a review is recorded with the admin token", `ADR-0012:159`) when it is accepted.
    - The LP-19 entry in `tasks/todo.md` gains the conflicts and owner decisions below, written
      before any build, as the prompt requires.
  - **Files:** new: the plan, the ADR. Changed: `mkdocs.yml` (the ADR's nav entry, beside
    ADR-0012's at `mkdocs.yml:97`), `tasks/todo.md`, `CHANGELOG.md`.
  - **Tests:** none new; `tests/test_threat_model.py` and the docs build stay green.
  - **Safe alone because:** documents only.

- [ ] **19b** Owned publisher tokens in the registry's store, and the operator's commands for
  them (requirement 1, store side).
  - **What it builds:**
    - A new table, never a new column (fact 3):
      `hub_publisher_tokens (id, token_sha256 UNIQUE, namespace, org, owner, name, created_at,
      expires_at, revoked_at, revoked_by)`. `id` is `token_id` (`registry.py:207-210`), so the
      chain and the rate buckets name a token as they do today.
    - `hub_token_use (token_id PRIMARY KEY, last_used_day)`: the last day a token was used,
      written at most once a day per token, **outside the chain** (as `rate_limits` is,
      `core/rate_buckets.py:8`). A day, not a time, so a publish costs no extra write.
    - `RegistryStore.mint_token(namespace, *, org, owner, name, days, by)`,
      `tokens(org=None, namespace=None)`, `revoke_token(id, *, org, by)` and
      `publisher(token) -> {id, namespace, org, owner} | None` (None when revoked, expired, or
      unknown; one query, as `AuditStore.api_key` is, `core/audit.py:3494-3502`).
    - Chained events: `token.issued` gains `org`, `owner` and `expires_at`; new `token.revoked`
      (the row re-hashed, as `namespace.publisher_type_set` does, `registry.py:326-342`). `ROWS`
      gains `publisher_tokens`.
    - `require_publisher` (`registry.py:1431-1436`), `known` (`:1376-1380`), `actor` and the
      report route accept an owned token. A legacy `hub_tokens` row and the admin token keep
      working in this PR; 19g ends both.
    - Operator commands on the registry's database, for a registry with no sign-in (a
      self-hosted `mendari registry serve`, CI, the seed script): `mendari registry token
      --db … --namespace … --org … --reason … [--days N]` (prints the token once),
      `mendari registry tokens --db …`, `mendari registry revoke-token --db … ID --reason …`.
      The owner recorded is `operator`, with the reason, as `mendari access grader-key` records
      its own (`core/audit.py:3359-3366`).
  - **Files:** `src/mendari/hub/registry.py`, `src/mendari/cli.py` (beside `registry serve` and
    `verify`, `cli.py:1763-1789`, `:3408`), `docs/operations/registry.md`. New:
    `tests/test_registry_tokens.py`.
  - **Tests** (`tests/test_registry_tokens.py`):
    - `::test_a_token_names_its_owner_organization_and_expiry_and_never_its_secret`;
    - `::test_an_expired_token_is_refused` (the clock moved, not a sleep);
    - `::test_a_revoked_token_is_refused_at_once_and_revoking_is_chained`;
    - `::test_a_token_publishes_only_to_its_namespace`;
    - `::test_using_a_token_records_the_day_and_the_chain_still_verifies`;
    - `::test_a_token_lives_1_to_365_days`;
    - `::test_another_organization_neither_lists_nor_revokes_it`;
    - `::test_the_operators_token_records_operator_and_why`;
    - `::test_a_database_from_before_owned_tokens_still_verifies` (a database made by main's
      schema opens, verifies, and its old rows hash as before).
  - **Safe alone because:** additive. No existing route, token or answer changes; a database
    from before opens and verifies.
  - **Threat model:** S22 (first half), R6 (first half).

- [ ] **19c** Namespace claims, reserved names, and a first publish that stays invisible until it
  is approved (requirements 2 and 3, store side).
  - **What it builds:**
    - `hub_claims (namespace PRIMARY KEY, org, claimed_by, created_at, status, decided_by,
      decided_at, note)`, status `pending`, `approved` or `refused`. The claim is the
      organization's link to the namespace (requirement 3).
    - `RegistryStore.claim(name, *, org, by, display_name)`: refuses a reserved name
      (`mendari`, `openhealth`, and any name that starts with `mendari-` or `openhealth-`), a name
      that exists (409), and an organization that already holds 3 pending claims (the bound
      `core/accounts.py` puts on open requests to join is the precedent). It creates the
      namespace row as `community` and the claim as `pending`, in one transaction, chained
      `namespace.claimed`.
    - With a pending claim, the publisher works as usual: registers keys, mints tokens,
      publishes, and the regression check runs. Nothing about it is served to anyone else.
    - `RegistryStore.decide_claim(name, *, approve, by, note)`: approval needs at least one
      published version (there is nothing to review otherwise); chained `namespace.approved`
      or `namespace.refused`. A refused namespace stays taken and never lists; releasing a
      name is the operator's and is not built here.
    - **One rule for every read**, in the store, not per route: a namespace is *listed* when its
      claim is approved, or it has no claim row (a namespace from before LP-19). `latest`,
      `versions`, `version`, `archive`, `namespace`, `reports`, `recalled` and so search, the
      recall list and `health`'s count take a `viewer`: nobody (the public), a namespace (its
      own token, which reads its own pending state so `mendari push` can confirm), or a
      reviewer. To the public a pending or refused namespace answers exactly as a namespace
      that does not exist (the same 404 body), so it cannot be enumerated.
    - `pack.published` gains `org` and `listed`. After approval, later versions and new pack
      names in the namespace list at once: the regression check, recalls, and each deploying
      organization's own approval still stand between a version and automation.
    - Operator commands: `mendari registry claim --db … --namespace … --org … --reason …`
      (the only way to create `mendari` and `openhealth`), `mendari registry approve|refuse
      --db … --namespace … --reason …`, `mendari registry claims --db …`.
  - **Files:** `src/mendari/hub/registry.py`, `src/mendari/cli.py`,
    `docs/operations/registry.md`. New: `tests/test_registry_claims.py`.
  - **Tests** (`tests/test_registry_claims.py`):
    - `::test_a_reserved_namespace_is_never_claimed` (parametrized over the reserved names);
    - `::test_a_claimed_name_is_not_claimed_twice`;
    - `::test_an_organization_holds_three_pending_claims_at_most`;
    - `::test_a_first_publish_is_invisible_to_search_pull_the_namespace_the_recall_list_and_health`;
    - `::test_a_pending_namespace_answers_as_one_that_does_not_exist` (the 404 bodies are equal);
    - `::test_its_own_token_reads_its_pending_version`;
    - `::test_approved_it_is_listed_and_pulls` (a real `hub.client.pull`);
    - `::test_a_refused_namespace_never_lists_and_its_archive_is_kept`;
    - `::test_a_claim_without_a_publish_cannot_be_approved`;
    - `::test_claim_approval_and_publish_are_chained_with_the_organization`;
    - `::test_a_namespace_from_before_claims_stays_listed`;
    - `tests/test_registry_chain.py::test_a_changed_chain_or_a_row_written_past_the_api_does_not_verify`
      gains a planted claim and a claim flipped to `approved`.
  - **Safe alone because:** a namespace with no claim row reads as today, so every existing
    namespace, test and seeded registry is unchanged; only a namespace made by the new `claim`
    is ever hidden. Hiding only lowers what is served.
  - **Threat model:** S23, I19 (enumeration), R6 (second half).

- [ ] **19d** The registry mounted under the engine, and the people's routes for tokens and
  claims (requirements 1 to 4, engine side). **This PR gets one adversarial security review.**
  - **What it builds:**
    - `MENDARI_REGISTRY` (`on`; unset or `off`: nothing mounted), read in `create_app` beside the
      other startup settings (`server/app.py:861-915`); a bad value stops the server there.
    - `create_registry_app` takes a store it is given, so the engine and the mounted app share
      one `RegistryStore` on `db_path` and `migrate_url` (`server/app.py:917`). The engine
      imports `mendari.hub.registry` inside `create_app` only (fact 10).
    - `app.mount("/registry", registry_app)`: the public reads and the token-authenticated
      writes at `/registry/v1/...`. One rule, `("*", r"^/registry/", "public")`, in `_RULES`:
      the engine's guard reads no credential there (fact 5), and the registry checks its own
      `ohr_` token. **A session cookie is never a publish credential**, so no page can make a
      signed-in browser publish. The registry's own limiter and headers apply
      (`registry.py:1389-1406`); the engine's header middleware only fills what is missing
      (`setdefault`, `server/app.py:3088-3098`), so the registry's stricter CSP stays. The
      archive answer gains `Content-Disposition: attachment`.
    - New module `src/mendari/server/registry_routes.py`, mounted only with sign-in on, people
      only, acting for the organization the guard let through (`org_of`), never one a body names:
      - `GET /api/registry/namespaces`: this organization's claims and their status;
      - `POST /api/registry/namespaces` `{name, display_name}`: claim;
      - `POST /api/registry/namespaces/{namespace}/keys`: register a publisher key;
      - `GET /api/registry/tokens`, `POST /api/registry/tokens` `{namespace, name, days}` (the
        token is in this answer only), `DELETE /api/registry/tokens/{id}`.
      Rule: `("*", r"^/api/registry/(namespaces|tokens)(/|$)", "keys")`, before the catch-all
      read (`auth.py:337`). The chain names the person by id (`user:<hash>`), never by address
      (LP-12's conflict 9, `tasks/todo.md:2207-2209`). The security log gets
      `admin.registry_token.created` and `.revoked`, as keys do (`auth.py:1601-1603`).
    - `/api/auth/me` (or the health answer a signed-in person reads) says the registry is on,
      so the dashboard knows whether to show its tabs. Anonymous `/api/health` keeps its three
      fields.
    - The mounted routes join the OpenAPI document and `tests/test_shipped.py`'s inventory.
  - **Files:** `src/mendari/server/app.py`, `src/mendari/server/auth.py` (`_RULES` only),
    `src/mendari/server/registry_routes.py` (new), `src/mendari/hub/registry.py`
    (`create_registry_app`'s signature), `src/mendari/server/vercel.py` (its settings list,
    `vercel.py:1-64`), `src/mendari/server/api_docs.py`, `tests/test_shipped.py`,
    `docs/guides/api.md`, `docs/operations/registry.md`. New: `tests/test_registry_mount.py`.
  - **Tests** (`tests/test_registry_mount.py`, signed in as `tests.conftest.member` makes
    sessions):
    - `::test_without_the_switch_nothing_is_mounted_and_no_hub_table_is_made`;
    - `::test_a_member_claims_mints_publishes_and_only_to_the_claimed_namespace` (the prompt's
      first test, over HTTP end to end);
    - `::test_a_session_cookie_is_not_a_publish_credential`;
    - `::test_an_engine_key_is_refused_by_the_registry_and_a_publisher_token_by_the_engine`;
    - `::test_a_key_or_a_client_cannot_mint_a_publisher_token` (people only);
    - `::test_a_viewer_cannot_claim_or_mint`;
    - `::test_one_organization_never_sees_or_revokes_anothers_tokens_or_claims` (the pattern of
      `tests/test_two_organizations.py`);
    - `::test_a_person_of_no_organization_is_refused`;
    - `::test_public_reads_need_no_sign_in_and_carry_the_registrys_headers`;
    - `::test_an_archive_over_the_engines_body_limit_is_a_413_that_names_it`;
    - `::test_the_registry_chain_and_the_audit_chain_both_verify_in_one_database`;
    - `::test_another_sites_page_cannot_claim_or_mint` (the origin check, `server/origin.py`);
    - `tests/test_shipped.py`: the six new routes in `CHAINED` with their events.
  - **Safe alone because:** off unless `MENDARI_REGISTRY=on`; the first test proves the engine
    is unchanged without it. No existing rule, role or route changes.
  - **Needs the owner's yes before it is turned on anywhere:** it puts publisher bytes and a new
    unauthenticated read surface on the engine's origin and database.
  - **Threat model:** S22 (second half: credential confusion and the cookie), I19 (the origin),
    D15.

- [ ] **19e** Registry reviewers: the queue and the acts that were the admin token's
  (requirement 2's "until a reviewer approves it"). **Adversarial security review.**
  - **What it builds:**
    - `MENDARI_REGISTRY_REVIEW_ORG`: the one organization whose reviewers are the registry's.
      Unset: no one reviews through the web. It must name an organization with a record, or the
      server says so at startup and leaves reviewing off (an optional feature never stops the
      server; the `CRON_SECRET` precedent, `server/app.py:893-901`).
    - A reserved membership role `registry_reviewer` (`core/accounts.py:35-39`) that grants the
      new people-only permission `registry_review` **only in the review organization**
      (`Auth._member`, near `auth.py:1045`). Granting it in any other organization is refused
      (422) at invitation and at role change (`auth.py:785-836`), so an administrator elsewhere
      can never make a reviewer. Memberships mode only, as `labeller` is; never open mode
      (`NEVER_OPEN`, `auth.py:194`), never a key.
    - Routes under `("*", r"^/api/registry/review(/|$)", "registry_review")`:
      - `GET /api/registry/review`: pending claims, each with its organization, who claimed it
        (by id), its versions' `checked` and `claimed` groups and the registry's rendered card;
      - `POST /api/registry/review/namespaces/{namespace}` `{decision, note}`: approve or
        refuse. A reviewer never decides a claim they filed themselves;
      - `POST /api/registry/review/namespaces/{namespace}/publisher-type`;
      - `POST /api/registry/review/packs/{namespace}/{name}/{version}` `{risk_level, note}`:
        the review LP-18b records (`registry.py:648-697`), now by a person;
      - `POST …/{version}/recall` `{reason, severity}`: the registry's signed recall;
      - `POST …/{version}/delete` `{reason}`.
      Each is chained by the store method that exists today, with `by` the reviewer's id.
    - Operator commands for the same acts where there is no sign-in or no review organization:
      `mendari registry publisher-type|review|recall|delete --db … --reason …` (19c already
      has `approve` and `refuse`).
  - **Files:** `src/mendari/core/accounts.py`, `src/mendari/server/auth.py`,
    `src/mendari/server/registry_routes.py`, `src/mendari/cli.py`, `tests/test_shipped.py`,
    `tests/test_memberships.py` (the role table), `docs/operations/accounts.md`,
    `docs/operations/registry.md`. New: `tests/test_registry_review.py`.
  - **Tests** (`tests/test_registry_review.py`):
    - `::test_a_first_publish_stays_invisible_until_a_reviewer_approves_it` (the prompt's
      second test, over HTTP);
    - `::test_an_administrator_of_another_organization_is_not_a_reviewer`;
    - `::test_the_reviewer_role_cannot_be_granted_outside_the_review_organization`;
    - `::test_without_a_review_organization_nobody_reviews_and_nothing_lists`;
    - `::test_a_reviewer_does_not_decide_their_own_claim`;
    - `::test_a_key_a_client_open_mode_and_claims_mode_never_review`;
    - `::test_approval_refusal_review_recall_and_deletion_are_chained_by_the_reviewers_id`;
    - `::test_the_queue_shows_what_the_registry_checked_apart_from_what_is_claimed`;
    - `::test_the_recall_needs_the_signing_key_and_says_so`.
  - **Safe alone because:** off without both settings; the admin-token routes still exist
    until 19g, so no registry loses a path in this PR.
  - **Needs the owner's yes:** who the review organization is (decision 2).
  - **Threat model:** E17, R6 (reviews).

- [ ] **19f** The dashboard: namespaces and publisher tokens in the Access view, and the
  reviewer's queue.
  - **What it builds:**
    - `src/mendari/dashboard/registry.js` (an ES module; `admin.js` is 1,339 lines already). It
      adds two tabs to `ADMIN_TABS` (`admin.js:29-38`), shown when the registry is on and the
      person holds `keys` (`credentials`, `admin.js:26`):
      - **Namespaces**: this organization's claims with their status in words ("Waiting for
        the registry's review", "Listed", "Refused"), a claim form, and a key form;
      - **Publisher tokens**: name, namespace, who made it, created, expires, last used (a
        day, or "never"), status; mint (the token shown once, through the existing one-time
        dialog, `showOnce`, `admin.js:443`) and revoke with a confirmation.
    - A **Registry review** view for a person who holds `registry_review`: the queue, each item
      with the checked and claimed groups side by side, the card as plain text, and Approve and
      Refuse with a one-line note.
    - Styles from tokens only (`CLAUDE.md:51`); no new colour. Empty, loading, refused and
      error states for each list; every control reachable by keyboard and named for a screen
      reader (`tests/browser/test_accessibility.py` covers the new views).
  - **Files:** `src/mendari/dashboard/registry.js` (new), `admin.js`, `main.js`, `router.js`,
    `index.html`, `style.css`. New: `tests/browser/test_registry_accounts.py`, on the
    memberships-mode site `tests/browser/access_site.py` already runs (`:27-58`).
  - **Tests** (`tests/browser/test_registry_accounts.py`, marked `browser`):
    - `::test_from_a_claim_to_an_approved_pack` (the prompt's browser flow: an administrator
      claims and mints in one window; the test publishes with that token over HTTP; search
      shows nothing; a reviewer approves in another window; search and a pull show it);
    - `::test_a_token_is_shown_once_and_never_again`;
    - `::test_a_revoked_token_says_so_and_no_longer_publishes`;
    - `::test_a_reserved_name_is_refused_in_words`;
    - `::test_only_reviewers_see_the_queue_and_only_administrators_the_tabs`;
    - `::test_without_the_registry_the_tabs_are_not_there`.
  - **Safe alone because:** the tabs appear only when the server says the registry is on.

- [ ] **19g** The shared admin token removed (the prompt's "Do not keep the shared admin token
  as a publishing path"; the owner's "the shared admin token goes").
  **Breaking for anyone who runs a registry.**
  - **What it builds:**
    - `create_registry_app` loses `admin_token`; `MENDARI_REGISTRY_ADMIN_TOKEN` is no longer
      read. Set, the registry logs once at startup that it is unused and names the commands
      that replace it.
    - Removed: `POST /v1/namespaces`, `PATCH /v1/namespaces/{namespace}`,
      `POST /v1/namespaces/{namespace}/tokens`, `POST …/review`, `POST …/delete`, the
      administrator's branch of `POST …/yank` (the publisher's signed recall stays), and the
      admin branches of `require_publisher`, `known`, `actor` and the report route
      (`registry.py:1376-1444`, `:1679`).
    - A legacy `hub_tokens` row no longer authenticates (it has no owner, expiry or
      revocation). Its row and its chained event stay, so the chain verifies.
    - The standalone registry (`mendari registry serve`) stays: public reads, publishes by
      owned token, everything else by the operator's commands on its database.
    - `scripts/hub_seed.py` seeds through the store (claim `mendari` as the operator, mint a
      token, approve after the publish); `--hostile-example` records its review the same way.
      The CI jobs `hub-site` and `hub-e2e` drop the variable (`ci.yml:327-334`, `:385-389`).
      `tests/conftest.py`'s `registry` fixture (`:308-327`) builds its namespace and token
      through the store.
  - **Tests that change** (the admin token appears in `tests/test_registry.py`,
    `test_registry_limits.py`, `test_hub_client.py`, `test_registry_chain.py`,
    `test_registry_yank.py`, `test_shipped.py:176-185,305` and `conftest.py:281-327`): each
    keeps its assertion and gets its namespace, token, review or deletion from the store or the
    operator's command. No assertion about what a version shows changes. New, in
    `tests/test_registry_tokens.py`:
    - `::test_no_shared_secret_publishes_creates_reviews_or_deletes` (every removed route is a
      404 or 405, with the old variable set);
    - `::test_a_token_from_before_owned_tokens_is_refused_and_the_chain_still_verifies`;
    - `tests/test_registry_limits.py::test_made_up_tokens_share_their_senders_bucket` keeps
      passing with the admin's own bucket gone.
  - **Files:** `src/mendari/hub/registry.py`, `scripts/hub_seed.py`,
    `.github/workflows/ci.yml`, `tests/` as listed, `hub/README.md`,
    `docs/operations/registry.md` (rewritten: line 11 says "Until LP-19 a registry has one
    administrator token"), `docs/security/threat-model.md` (R3 names `admin`; T7; D11),
    `CHANGELOG.md` (Breaking).
  - **Safe alone because:** every act the token did has a tested replacement merged before it
    (19b, 19c, 19e). No hosted registry exists (`docs/operations/registry.md:11`), so nothing
    deployed loses a path; a self-hosted one is told what to run instead.
  - **Needs the owner's yes:** it is a breaking change to a published interface.

- [ ] **19h** The client refuses plain http, except to this machine (requirement 5, second
  half). Parallel with 19b.
  - **What it builds:** `RegistryClient.__init__` (`hub/client.py:108-120`), and so `login`
    (`:84-93`), `default_registry` (`:96-105`) and `Decision.from_hub`
    (`core/decision.py:341-349`), refuse a URL that is not https unless its host is
    `localhost`, a `.localhost` name or a loopback address (the rule `server/origin.py:178-187`
    and `server/recalls.py:69-74` already use; moved to a module the base install can import,
    since `hub/client.py` must not need the server extra). The refusal comes before any request,
    so a token is never sent in clear. Redirects stay off (`client.py:117`).
  - **Files:** `src/mendari/hub/client.py`, a small shared helper under `src/mendari/core/`,
    `src/mendari/server/recalls.py` (uses the helper), `docs/operations/registry.md`.
  - **Tests** (`tests/test_hub_client.py`):
    - `::test_a_registry_is_reached_over_https_only_but_on_this_machine` (parametrized: http to
      a name, to a private address, to `localhost`, to `127.0.0.1`, to `[::1]`; a URL with a
      user and password);
    - `::test_no_token_is_sent_to_a_refused_address` (a transport that fails the test if called);
    - `::test_a_saved_http_registry_is_refused_with_what_to_do`.
    The existing tests already use `https://registry.example` or a loopback port
    (`test_hub_client.py:251-253`, `:40-44`).
  - **Safe alone because:** it only refuses. A local registry keeps working.
  - **Threat model:** I20.

- [ ] **19i** The registry counter-signs what it accepts, and the client checks it against a
  shipped or pinned key (requirement 5, first half).
  - **What it builds:**
    - `hub/acceptance.py`: a record `mendari.registry-acceptance/1`
      `{format, pack, version, digest, archive_sha256, publisher_key_id, key_id, at, signature}`,
      Ed25519 over the canonical JSON of the other fields, as a recall is signed
      (`hub/yank.py:96-131`), with a `format` of its own so no recall, checkpoint or scoreboard
      signature can be replayed as one (S20's rule).
    - `hub_acceptances (namespace, name, version, record, created_at)`, chained `pack.accepted`.
      Written when a version becomes listed: at publish in a listed namespace, or at approval
      for each version published while the claim was pending. A version of a refused namespace
      is never counter-signed. Without `MENDARI_REGISTRY_SIGNING_KEY`, none is written.
    - Served as `checked.accepted` (the record, or null) beside the other checks
      (`registry.py:1050-1076`).
    - `mendari registry countersign --db …`: signs every listed version that has no record
      (versions from before 19i, or published while the key was unset). Idempotent.
    - `src/mendari/hub/registry_keys.json`, in the wheel:
      `{"format": "mendari.registry-keys/1", "keys": []}`. It ships empty, as
      `scoreboard/keys.json` does; the owner's public key goes in by a later one-line PR (owner
      step 2). It holds keys only, never a hostname.
    - The client (`hub/client.py`, in `pull` before the archive is fetched, `:266-271`): the
      registry's key is trusted when it is a shipped key, or the one pinned for that URL on
      first use (`_registry_key`, `:375-398`, unchanged). Then:
      - the registry has a trusted key and the version's record verifies and names exactly
        this pack, version, digest, archive and publisher key: pulled, and the origin marker
        records `accepted: true` (`_write_origin`, `:292-310`);
      - it has a trusted key and the record is missing, does not verify, or names something
        else: **refused**, nothing cached;
      - the registry publishes no key and none was ever pinned for it (a local registry):
        pulled as today, the marker records `accepted: null`, and `mendari pull` says the
        version is not counter-signed;
      - a key was pinned and the registry now publishes none or another: refused, as a changed
        host key is.
    - The Hub shows "Counter-signed by the registry" or "Not counter-signed" in the checked
      group (`hub/lib/registry.ts:10-26`, `hub/app/packs/[namespace]/[name]/page.tsx`).
  - **Files:** `src/mendari/hub/acceptance.py` (new), `registry_keys.json` (new),
    `src/mendari/hub/registry.py`, `src/mendari/hub/client.py`, `src/mendari/cli.py`,
    `pyproject.toml` (the data file, if the wheel target needs it named), `hub/lib/registry.ts`,
    the pack page, `hub/tests/smoke.spec.ts`. New: `tests/test_registry_acceptance.py`.
  - **Tests** (`tests/test_registry_acceptance.py`):
    - `::test_an_accepted_version_carries_a_record_that_verifies`;
    - `::test_a_version_planted_in_the_database_has_no_record_and_is_not_pulled`;
    - `::test_a_record_for_another_version_digest_archive_or_key_is_refused` (parametrized);
    - `::test_a_recall_or_a_checkpoint_signature_is_not_an_acceptance`;
    - `::test_a_pending_version_is_signed_at_approval_and_a_refused_one_never`;
    - `::test_a_registry_that_drops_or_changes_its_key_is_refused`;
    - `::test_a_shipped_key_is_trusted_without_first_use` (a keys file passed in by the test);
    - `::test_without_a_key_pull_works_and_says_not_counter_signed`;
    - `::test_countersign_signs_what_was_published_before_and_twice_changes_nothing`;
    - `::test_the_shipped_file_holds_public_keys_only`.
  - **Safe alone because:** with no signing key set (every registry today), nothing is signed
    and the client behaves as on main. Refusing only lowers what is pulled.
  - **Needs the owner's yes:** it changes what a client refuses once a registry has a key
    (decision 6).
  - **Threat model:** T11.

- [ ] **19j** Deployment configuration for option A (requirement 4). Configuration only.
  - **What it builds:**
    - **The engine project:** no new function setting is needed (fact 11). `vercel.json` keeps
      `maxDuration: 300`; a test pins that it stays above the regression check's limit plus a
      margin. `src/mendari/server/vercel.py`'s settings list names `MENDARI_REGISTRY`,
      `MENDARI_REGISTRY_REVIEW_ORG`, `MENDARI_REGISTRY_SIGNING_KEY`,
      `MENDARI_REGISTRY_RATE_LIMITS` and `MENDARI_REGISTRY_REGRESSION`.
    - **The Hub project:** `hub/vercel.json` (the framework, and an ignored-build step so a
      commit that does not touch `hub/` builds nothing). The project itself, its root directory
      (`hub`), its `REGISTRY_URL` (the engine's public origin plus `/registry`) and its domain
      are owner steps, written in `deploy/vercel/README.md`, with no hostname.
    - `deploy/vercel/README.md`: a "Registry and Hub" part: the order to turn things on, the
      firewall staying off `/registry` for the same reason it stays off `/api`
      (`deploy/vercel/README.md:5-9`), what to watch (function duration on publish, database
      size from archives), and how to turn it off (unset `MENDARI_REGISTRY`, redeploy: the
      rows stay).
    - `deploy/onprem/docker-compose.yml` and `deploy/kubernetes/mendari.yaml`: the setting,
      commented off; `tests/test_deploy.py::test_every_setting_the_templates_use_is_documented`
      covers it.
    - `deploy/status/checks.yaml` and `docs/operations/status-checks.md`: the registry check's
      base is `{api}/registry` under option A (the check itself, `GET {registry}/v1/health`,
      is unchanged).
    - `docs/operations/registry.md`: "The hosted shape", and the runbook lines (backup is the
      engine's; `mendari registry verify` beside `mendari audit verify`; keeping the chain's
      head elsewhere, `registry.py:1446-1450`).
  - **Tests:** `tests/test_vercel.py`:
    - `::test_the_function_outlasts_a_publishs_regression_check`;
    - `::test_the_engine_function_ships_the_registry_and_not_the_hub_site` (the `excludeFiles`
      pattern, `vercel.json:16`, matches the root `hub/` and not `src/mendari/hub/`);
    - `::test_the_hubs_project_file_names_no_host_or_project`;
    - `tests/test_status_checks.py::test_the_registry_check_reads_the_mounted_registry`;
    - `tests/test_public_tree.py` stays green.
  - **Safe alone because:** files a deployment reads only when the owner creates the project or
    sets the variable. Nothing is deployed.
  - **Needs the owner's yes:** every owner step below.

- [ ] **19k** Close-out.
  - `launch/gates.yaml`:
    - `a10-registry-hub` (`:469`): the pending line becomes the tests of 19b to 19i (owned
      tokens, claims, the mount, reviewers, the browser flow, no shared secret, https, the
      counter-signature) and one line the owner attests: the registry mounted and the Hub
      deployed. The prerequisite stays unmet until that attestation, honestly.
    - `status-page` (`:558`): reworded to what is left, all the owner's (the registry and Hub
      hosted, a status organization); 19j's status-check test joins its tests.
    - `:403` (LP-17l) names LP-20 alone; `:598` (LP-22) drops LP-19.
  - ADR-0015 to Accepted once the owner confirms the decisions; ADR-0012's status and its
    default 5 amended (`ADR-0012:8-11`, `:78`, `:159`).
  - `docs/security/threat-model.md`: the eight rows, each naming its tests; R3, T7 and D11
    reworded (no `admin`); the residual risks: the chain is still unkeyed, a refused name stays
    taken, site reports are still unverified (LP-22).
  - `docs/guides/authoring-a-pack.md` and `docs/guides/api.md`: how to claim, mint and
    publish; the `/docs` landing if it names the registry.
  - `tasks/todo.md`: LP-19 checked, the owner steps listed, LP-20 and LP-22 unblocked on it.
  - `tests/test_gates.py` and `tests/test_shipped.py` green; `NOT_CHAINED_YET` still empty.

## Which pending probes each PR replaces

| `launch/gates.yaml` | Today | Replaced by |
|---|---|---|
| `:469` a10-registry-hub | pending: LP-19 | 19k, with tests from 19b (`test_registry_tokens.py`), 19c (`test_registry_claims.py`), 19d (`test_registry_mount.py`), 19e (`test_registry_review.py`), 19f (`tests/browser/test_registry_accounts.py`), 19g, 19h, 19i; plus an owner attestation for the deploy |
| `:558` status-page | pending: LP-19 and a status organization | 19k rewords it to the owner's remaining steps; 19j adds the mounted-registry check test |
| `:403` LP-17l | pending: after LP-19 and LP-20 | 19k rewords: after LP-20 |
| `:598` a11-site-network | pending: LP-22, after LP-07, LP-10 and LP-19 | 19k rewords: LP-19 is built |

## Conflicts written before building, and the default each PR follows

1. **"Tokens belong to a signed-in user", and a registry with no sign-in.** A self-hosted
   `mendari registry serve`, CI and the seed script have no one signed in. Default: the
   operator's commands on the database mint a token (owner `operator`, with a reason), as the
   operator mints a grading key. The web path needs sign-in. (19b)
2. **One approval or two.** The prompt has a claim and a first publish; the owner's note has
   "claims and their approval". Default: one decision, taken when there is a published version
   to look at. A claim alone only holds the name, pending and invisible, capped at three per
   organization. (19c, 19e)
3. **"Last used".** The engine's API keys do not record it
   (`core/audit.py:362-376`). And the registry's chain refuses a row that changes without an
   event (`registry.py:871-890`). Default: publisher tokens record the last *day* used, in a
   table outside the chain. The engine's own keys are not changed by LP-19. (19b)
4. **Who is a reviewer.** Roles belong to a membership in one organization, and any
   administrator can grant any membership role (`core/accounts.py:35-39`). A plain new role
   would let every organization approve itself. Default: the role counts only in the one
   organization `MENDARI_REGISTRY_REVIEW_ORG` names, and cannot be granted elsewhere.
   Memberships mode only; in claims mode and open mode the operator's commands review. (19e)
5. **Archive size.** The registry takes 100 MB (`hub/archive.py:52`); under the engine every
   body is capped at `MENDARI_MAX_BODY_BYTES`, 4 MiB by default, and Vercel refuses more than
   4.5 MB whatever the app says (`settings.py:111-114`). Default: under the mount the engine's
   limit applies and the 413 says so. Larger archives are LP-20's (the pack store). The
   reference packs are about 1.2 MB each before compression. (19d)
6. **"A longer function timeout".** The owner's note expects one; `vercel.json` already gives
   every request 300 s. Default: no change, and a test that pins it. Whether the regression
   check's child process runs on the platform at all could not be checked here (open point 1);
   if it cannot start, the check says `failed: the check could not start`
   (`hub/regression.py:126-127`) and the publish still succeeds, as LP-18g decided. (19j)
7. **"A registry key shipped with the client" and "no production hostname in the tree".** A key
   bound to a hostname would put the hostname in the tree. Default: the shipped file holds
   public keys only; a registry that presents one of them is trusted without first use. The
   private key is the owner's, so the file ships empty until the owner makes it. (19i)
8. **Where the events are chained.** Definition of shipped 5 asks for a chained event bound to
   its organization. Default: the registry's own chain (`hub_events`), each new event carrying
   `org`. They do not appear in the Access view's Activity tab, which reads the engine's chain;
   the registry's tabs show who made and revoked what. One chain per store, no event written
   twice. (19b to 19e)
9. **The admin token: the publishing path, or all of it.** The prompt forbids keeping it as a
   publishing path; the owner's option A says it goes. Default: all of it goes, in one PR, after
   every replacement is merged. (19g)
10. **A session is not a publish credential.** One origin means a signed-in browser reaches the
    registry's routes. Default: the mounted registry never reads the session cookie; publishing
    is by `ohr_` token only; minting is a people-only engine route under the origin check. (19d)
11. **Namespaces and tokens from before LP-19.** Default: a namespace with no claim stays
    listed (nothing published disappears); a token with no owner works until 19g and is refused
    after it. No hosted registry exists, so this touches local databases only. (19c, 19g)

## Owner steps (not done by any PR)

1. Set `MENDARI_REGISTRY=on` on the engine project, after 19d to 19g are merged and reviewed.
2. Make the registry's key: `openssl rand -base64 32`, set as `MENDARI_REGISTRY_SIGNING_KEY`
   (a secret, for the environment that serves the registry only); then send its public half
   (`GET /registry/v1/registry-key`) for the one-line PR that ships it in `registry_keys.json`.
3. Name the review organization (`MENDARI_REGISTRY_REVIEW_ORG`) and give its reviewers the role
   in the Access view.
4. Claim `mendari` and `openhealth` with `mendari registry claim` and publish the reference
   packs under an owned token; run `mendari registry countersign` once.
5. Create the Hub's Vercel project (root directory `hub`), set its `REGISTRY_URL`, and its DNS.
   DNS for a registry host of its own, if wanted, is a setting on the engine project.
6. Add the registry and Hub checks to the status provider, then attest the two gate lines.

## Owner decisions still open (each PR follows the default until told otherwise)

1. **The mount path.** Default: `/registry` on the engine's origin. A host of its own on the
   same project is a domain setting the owner adds; no code depends on it.
2. **The review organization, and whether a second role is wanted.** Default: one organization,
   one role `registry_reviewer`. Alternative: its `governance` members review, with no new role.
3. **Self-approval.** Default: a reviewer never decides a claim they filed; the operator's
   command is the way out for a one-person team, with a reason on the chain. Alternative: also
   refuse a reviewer whose own organization holds the claim (stricter; it blocks the owner's own
   namespaces until a second organization reviews).
4. **Who may claim and mint.** Default: an organization's administrators (`keys`). Alternative:
   wait for LP-20 to give `pack_author` its permissions and let that role mint.
5. **What happens to a person's tokens when they leave the organization.** Default: the token is
   the organization's and keeps working; it is listed with who made it, and the revoking-access
   runbook says to revoke it. Alternative: revoke on removal.
6. **A version with no counter-signature, on a registry that has a key.** Default: refused by
   the client. Alternative: pulled with a warning for one release.
7. **Is the organization shown on the Hub.** Default: no. The public answer says only that the
   namespace is linked to an organization; its id and name are for the organization and the
   reviewers. Alternative: show the organization's name.
8. **Reserved names.** Default: `mendari`, `openhealth` and names that start with either plus a
   dash. Other lookalikes are the reviewer's to refuse.
9. **Operator commands against the hosted database.** Default: allowed, as `mendari access`'s
   break-glass is, each with a reason on the chain.
10. **After approval, do new pack names in a namespace list at once.** Default: yes.

## Left to LP-20 and LP-22, on purpose

- **LP-20 (pack store):** who a submitting developer is, what `pack_author` grants, archives
  larger than a function's body limit, object storage for archives, paid or private packs.
- **LP-22 (site network):** site keys and signed site reports. LP-19 only records which
  organization a reporting token belongs to; reports stay "unverified"
  (`registry.py:633-639`).
- **Not in LP-19:** releasing a refused or abandoned name; transferring a namespace between
  organizations; rotating a publisher token (mint a new one, revoke the old); a keyed registry
  chain or a signed checkpoint of its head (the signed checkpoint was brought forward by the
  owner on 2026-11-10 and built after 19i: `hub/checkpoint.py`; a keyed chain is still not
  in LP-19); e-mail to claimants (Mendari sends none).

## Risks

1. **The registry shares the engine's functions and database.** A publish holds a function for
   up to three minutes and archives grow the database. Kept in check by the registry's own
   limits (`registry.py:1305-1309`), two checks at once per instance
   (`hub/regression.py:42`), and the body limit. The switch turns it off without losing rows.
2. **Publisher bytes on the dashboard's origin.** Answers are JSON or an attachment, under a
   CSP that loads nothing and `nosniff`; 19d's tests assert each header on each kind of answer.
   This is the surface the external review (gate `external-review`) should see first.
3. **Two connection pools on one Postgres.** Each instance opens a second pool for the
   registry's store. On SQLite the two stores wait on each other's write lock (30 s timeout,
   `core/db.py:124-126`); a publish's write is short, its check runs outside the transaction
   (`registry.py:417-427`, then `:444`).
4. **A chain that an old database must still pass.** New tables, never new columns, so old rows
   hash as before; 19b's last test opens a database made by main's schema.
5. **Reviewer by setting.** A wrong `MENDARI_REGISTRY_REVIEW_ORG` makes another organization the
   reviewers. It is read at startup, shown to a signed-in administrator in health, and changing
   it is a deploy the owner makes.
6. **19g is breaking.** It lands last among the registry PRs, with the replacements already
   tested, and the changelog says what to run instead.
7. **Parallel lanes and ids.** ADR-0015 and the threat rows were free at 78be63c9 only.

## Open points not verified by the planning read

1. **Whether the regression check's child process can start on Vercel's Python runtime**, and
   whether its 2 GiB memory limit (`hub/regression.py:38`) fits the function's memory. Nothing
   was run. If it cannot, hosted versions show "the check could not start" until it is moved.
2. **Whether `excludeFiles`' `hub/**` (`vercel.json:16`) matches only the root `hub/`.** The
   engine already imports `mendari.hub.client` lazily (`core/decision.py:341`,
   `core/registry.py:66`), which suggests it is shipped, but no build was inspected. 19j's
   test checks the pattern, not Vercel's matcher.
3. **How the request log names a path under a mount** (`security_log.RequestLog`,
   `server/app.py:3103-3110`, takes the app's own routes): whether `/registry/v1/packs/…` is
   logged as a template or raw. Not PHI either way, but 19d should look.
4. **Whether Node's `fetch` from the Hub sends fetch-metadata headers** that the engine's origin
   check reads as cross-site. A public GET passes either way (`server/origin.py:11-16`), so the
   Hub's reads should be unaffected; 19d's public-read test should send them to be sure.
5. **The compressed size of each reference pack's archive** against the 4 MiB limit: estimated
   from directory sizes (about 1.2 MB each), not built.
6. **The exact lines of `Auth._member`'s role resolution** (near `auth.py:1045`) and of the
   invitation and role-change validators (`auth.py:785-836`): read in outline, not line by line.
7. **`docs/operations/accounts.md`, `docs/guides/api.md` and the `/docs` landing** were not
   read; the plan names them as places to update, not what they say today.
8. **Whether the wheel already ships non-Python files under `src/mendari/hub/`**
   (`pyproject.toml:80-83` has a force-include list that was not read in full).
