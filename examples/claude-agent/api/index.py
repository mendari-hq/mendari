# SPDX-License-Identifier: Apache-2.0
"""The agent over HTTP (FastAPI): `POST /decide` for Mendari, `GET /health` for monitors.

Vercel serves this file's `app` as one function (vercel.json routes every path here); locally:
`uvicorn api.index:app --port 8787` from examples/claude-agent.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent

app = FastAPI(title="Mendari demo agent (Claude)", docs_url=None, redoc_url=None)
_client: Any = None


def messages() -> Any:
    """The Anthropic client's beta messages API, made once per instance."""
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic(max_retries=1, timeout=12.0)
    return _client.beta.messages


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent_version": agent.AGENT_VERSION}


@app.post("/decide")
async def decide(request: Request) -> JSONResponse:
    if not agent.authorized(request.headers.get("authorization")):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "the request is not JSON"}, status_code=400)
    try:
        return JSONResponse(agent.answer(body, messages()))
    except agent.AgentError as error:
        return JSONResponse({"error": error.message}, status_code=error.status)
    except Exception as error:  # the model's API: a status and a type, never a body
        status = getattr(error, "status_code", None)
        return JSONResponse(
            {"error": f"the model could not answer ({type(error).__name__}, {status})"},
            status_code=502,
        )
