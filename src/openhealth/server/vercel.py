"""Vercel entrypoint, imported by the root `app.py` shim that Vercel auto-detects.

Configuration comes from the environment:
- `DATABASE_URL`: pooled Postgres URL (injected by the Neon integration). Required on Vercel:
  functions have no persistent disk, and an audit trail that silently disappears is worse
  than none. `DATABASE_URL_UNPOOLED` (direct) is used for schema setup when present.
- `OPENHEALTH_PROVIDERS`: provider variants, e.g. `router=jev`.
- `OPENHEALTH_JEV_DAILY_LIMIT`, `OPENHEALTH_LLM_DAILY_LIMIT`: daily call budgets.
- `LLM_MODEL`: default model for `llm` variants (AI Gateway id, e.g. anthropic/claude-sonnet-5).
- `JEV_API_URL`, `JEV_MODEL`, `JEV_CONNECTOR`: see `openhealth.providers.jev`.
"""

from __future__ import annotations

import os

from openhealth.errors import OpenHealthError
from openhealth.sandbox import example_datasets_dir
from openhealth.server.app import create_app
from openhealth.settings import daily_limits, parse_providers


def database_target() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    if os.environ.get("VERCEL"):
        raise OpenHealthError(
            "DATABASE_URL is not set. Connect a Postgres database (e.g. Neon) to this Vercel "
            "project for Production and Preview, then redeploy."
        )
    return "var/openhealth.db"


app = create_app(
    db_path=database_target(),
    migrate_url=os.environ.get("DATABASE_URL_UNPOOLED", "").strip() or None,
    providers=parse_providers(None),
    datasets_dir=example_datasets_dir(),
    budget_limits=daily_limits(),
)
