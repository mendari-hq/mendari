# SPDX-License-Identifier: Apache-2.0
"""Vercel entrypoint, imported by the root `app.py` shim that Vercel auto-detects.

Configuration comes from the environment:
- `DATABASE_URL`: pooled Postgres URL (injected by the Neon integration). Required on Vercel:
  functions have no persistent disk, and an audit trail that silently disappears is worse
  than none. `DATABASE_URL_UNPOOLED` (direct) is used for schema setup when present.
- `MENDARI_PROVIDERS`: provider variants, e.g. `reader=rules`, or scoped to one pack,
  e.g. `mendari/portal-message-agent:reader=rules`. A setting that names a slot or
  variant a pack does not have is skipped and reported in `/api/health`. With Jev set up, a
  pack listed in `settings.JEV_DEFAULTS` (none in a release) runs Mendari + Jev in every slot
  these settings choose nothing for.
- `MENDARI_JEV_DAILY_LIMIT`, `MENDARI_LLM_DAILY_LIMIT`: daily call budgets (`none`: no
  limit). `MENDARI_ORG_DAILY_LIMITS`: per-organization daily quotas
  (`decisions=5000,jev=2000,llm=200`; unset: none). `MENDARI_PRINCIPAL_DAILY_LIMIT`: a daily
  cap on the decisions each principal asks for (unset: none).
- `MENDARI_DECISION_DEADLINE_S`: how long one decision's remote calls may take together
  (whole seconds, 10 to 270; default 120), within `maxDuration` (300 s, `vercel.json`).
- `MENDARI_RATE_LIMITS`: `on` (the default with sign-in), `log` or `off`, then overrides
  (`server/limits.py`). The client address is Vercel's `X-Forwarded-For`.
- `MENDARI_NOTICE_ACKNOWLEDGEMENT`: `required` (the default) or `off`: whether a signed-in
  person acknowledges the notice (`mendari.notices`) before taking data in.
- `MENDARI_TIMEZONE`: time zone for calendar days and displayed times (default
  `America/New_York`); stored timestamps stay UTC.
- `MENDARI_FHIR_VALIDATION`: US Core checks on FHIR inputs, `warn` (default: issues are
  returned and audited, decisions unchanged), `enforce` (a non-conforming input is blocked) or
  `off`.
- `MENDARI_FHIR_ALLOWED_SERVERS`: FHIR servers (origins, comma-separated) that CDS Hooks may
  fetch missing prefetch data from. Empty (default): nothing is fetched.
- `MENDARI_SMART_CLIENT_ID` (enables the SMART EHR launch), `MENDARI_SMART_CLIENT_SECRET`
  (confidential clients), `MENDARI_SMART_SCOPES`, `MENDARI_PUBLIC_URL` (this deployment's
  public origin, for the redirect URI).
- `MENDARI_OIDC_ISSUER`, `MENDARI_OIDC_CLIENT_ID` (switch on sign-in; unset: open, behind
  Vercel Authentication), `MENDARI_OIDC_CLIENT_SECRET`, `MENDARI_OIDC_ROLE_CLAIM`,
  `MENDARI_OIDC_ROLE_MAP`, `MENDARI_OIDC_ORG_CLAIM`, `MENDARI_OIDC_ENDPOINT_ORIGINS` (the
  provider's endpoints on other hosts, e.g. Google's). `MENDARI_ACCOUNTS`: `claims` (default)
  or `memberships` (organizations and roles from records, ADR-0008; the owner switches it,
  after `mendari access check` and the bootstrap: docs/operations/accounts.md).
- `MENDARI_PHI_KEY` and `MENDARI_INPUT_HASH_KEY`, for Production and Preview: the PHI mode
  here is `encrypt` unless `MENDARI_PHI_MODE=hash` (which needs only the hash key); `off` is
  refused.
- `CRON_SECRET` (16 characters or more): turns on the daily jobs in `vercel.json` (`crons`):
  the retention purge at 08:00 UTC (`/api/internal/retention`), the monitor at 09:00
  (`/api/internal/monitor`) and the checkpoint at 10:00 (`/api/internal/checkpoint`);
  `MENDARI_RETENTION_DAYS` (default 30); `MENDARI_STATUS_ORG` is the organization the monitor
  leaves out (the status checks').
- `MENDARI_REGISTRY`: `on` serves the pack registry under this app, on its database, at
  `/registry/v1/...`, with an administrator's claims and publisher tokens at
  `/api/registry/...` (LP-19d; docs/operations/registry.md). Unset (the default): nothing is
  mounted. A request body there is `MENDARI_MAX_BODY_BYTES` at most, under Vercel's 4.5 MB.
  `MENDARI_REGISTRY_REVIEW_ORG` names the one organization whose `registry_reviewer` members
  review it at `/api/registry/review/...` (LP-19e; unset: nobody does through the web).
  `MENDARI_REGISTRY_SIGNING_KEY` (32 bytes in base64) signs the registry's recalls and
  counter-signs what it lists (unset: neither); `MENDARI_REGISTRY_RATE_LIMITS` (`on` unless
  set, `log` or `off`, then overrides); `MENDARI_REGISTRY_REGRESSION` (`on` unless set, or
  `off`): a publish's regression check runs within this function's `maxDuration`
  (`vercel.json`, 300 s; the check stops at 180). The order to turn these on, and the Hub's
  own project, are in deploy/vercel/README.md ("Registry and Hub").
- `MENDARI_AGENT_STORE`: `on` serves organizations' pack stores at `/api/store/...` (LP-20f;
  memberships mode only, docs/operations/agent-store.md). Unset (the default): nothing is
  mounted. `MENDARI_STORE_ORG_BYTES` (256 MiB unset) bounds what one organization keeps;
  `MENDARI_STORE_MAX_ARCHIVE_BYTES` (32 MiB unset) an archive, sent in parts each under
  `MENDARI_MAX_BODY_BYTES`.
- `MENDARI_CHECKPOINT_KEY` (32 bytes in base64): signs the daily checkpoint, published at
  `/api/audit/checkpoint` (LP-21k).
- `MENDARI_ALERT_WEBHOOK` (where quota alerts go), `MENDARI_ALERT_FORMAT` (`mendari` or
  `slack`) and `MENDARI_ALERT_SECRET` (32 characters or more: signs every alert).
- `LLM_MODEL`: the model for `llm` variants that pin none (an AI Gateway id,
  `<vendor>/<model>`). There is no built-in default: without it they are `not_configured`, and
  their decisions go to a person.
- `JEV_API_URL`, `JEV_MODEL`, `JEV_CONNECTOR`: see `mendari.providers.jev`.
"""

from __future__ import annotations

import os

from mendari.sandbox import example_datasets_dir
from mendari.server.app import create_app
from mendari.settings import daily_limits, database_target, default_providers, parse_providers

# Built at import, as Vercel needs: anything that imports this module opens `database_target()`.
app = create_app(
    db_path=database_target(),
    migrate_url=os.environ.get("DATABASE_URL_UNPOOLED", "").strip() or None,
    providers=parse_providers(None),
    default_providers=default_providers(),
    datasets_dir=example_datasets_dir(),
    budget_limits=daily_limits(),
)
