# SPDX-License-Identifier: Apache-2.0
"""Vercel entrypoint, imported by the root `app.py` shim that Vercel auto-detects.

Configuration comes from the environment:
- `DATABASE_URL`: pooled Postgres URL (injected by the Neon integration). Required on Vercel:
  functions have no persistent disk, and an audit trail that silently disappears is worse
  than none. `DATABASE_URL_UNPOOLED` (direct) is used for schema setup when present.
- `MENDARI_PROVIDERS`: provider variants, e.g. `router=jev`, or scoped to one pack,
  e.g. `mendari/clinical-trial-prescreener:notes=jev`. A setting that names a slot or
  variant a pack does not have is skipped and reported in `/api/health`. With Jev set up, the
  reference packs run Mendari + Jev in every slot these settings choose nothing for
  (`settings.JEV_DEFAULTS`).
- `MENDARI_JEV_DAILY_LIMIT`, `MENDARI_LLM_DAILY_LIMIT`: daily call budgets (`none`: no
  limit). `MENDARI_ORG_DAILY_LIMITS`: per-organization daily quotas
  (`decisions=5000,jev=2000,llm=200`; unset: none).
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
  provider's endpoints on other hosts, e.g. Google's).
- `MENDARI_PHI_KEY` and `MENDARI_INPUT_HASH_KEY`, for Production and Preview: the PHI mode
  here is `encrypt` unless `MENDARI_PHI_MODE=hash` (which needs only the hash key); `off` is
  refused.
- `CRON_SECRET` (16 characters or more): turns on the daily retention purge (`vercel.json`
  crons, `/api/internal/retention`); `MENDARI_RETENTION_DAYS` (default 30).
- `LLM_MODEL`: default model for `llm` variants (AI Gateway id, e.g. anthropic/claude-sonnet-5).
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
