# SPDX-License-Identifier: Apache-2.0
"""The agent over HTTP (the kit's FastAPI app): `POST /decide` for Mendari, `GET /health`.

Vercel serves this file's `app` as one function (vercel.json routes every path here); locally:
`uvicorn api.index:app --port 8787` from examples/claude-agent.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent
from mendari_agent_kit import Case, create_app

_client: Any = None


def messages() -> Any:
    """The Anthropic client's beta messages API, made once per instance."""
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(max_retries=1, timeout=12.0)
    return _client.beta.messages


def handler(case: Case) -> dict[str, Any]:
    return agent.answer(case, messages())


app = create_app(
    handler, title="Mendari demo agent (Claude)", service_version=agent.SERVICE_VERSION
)
