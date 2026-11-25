# SPDX-License-Identifier: Apache-2.0
"""The agent over HTTP: `POST /decide` for Mendari, `GET /health` for monitors.

Vercel serves this file's `app` as one function (vercel.json routes every path here); locally:
`uvicorn api.index:app --port 8787` from examples/http-agent.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent
from mendari_agent_kit import create_app

app = create_app(
    agent.handler, title="Mendari agent (keyword rules)", service_version=agent.SERVICE_VERSION
)
