# SPDX-License-Identifier: Apache-2.0
"""The deployment's own credential for a remote provider: its Vercel OIDC token, which the
Vercel AI Gateway accepts in place of a key (`providers/llm.py`); `AI_GATEWAY_API_KEY`, when
set, is used instead and the token is not looked up.

Secrets are never logged and never appear in error messages or audit records.

Which environment variables supply a provider's endpoint, model and key is fixed in code and
belongs to the deployment. A pack's provider config may not name them: a pack that could would
choose which process secret is sent, and to which URL (`core/spec.py`, `DEPLOYMENT_ENV_KEYS`).
"""

from __future__ import annotations

from mendari.errors import ProviderError


def vercel_oidc_token() -> str:
    """The deployment's OIDC token (on Vercel), or the one `vercel env pull` wrote locally."""
    try:
        from vercel.oidc import VercelOidcTokenError, get_vercel_oidc_token_sync
    except ImportError:
        raise ProviderError(
            "credential_unavailable",
            "vercel-oidc is not installed: set AI_GATEWAY_API_KEY or install vercel-oidc",
        ) from None

    try:
        token = get_vercel_oidc_token_sync()
    except VercelOidcTokenError:
        token = ""
    if not token:
        raise ProviderError(
            "credential_unavailable",
            "no Vercel OIDC token: deploy on Vercel, or run `vercel env pull` for local use",
        )
    return token
