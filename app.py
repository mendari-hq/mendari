# SPDX-License-Identifier: Apache-2.0
"""Vercel entrypoint shim. Vercel auto-detects `app` in a root `app.py`; the real entrypoint,
with its configuration, is `mendari.server.vercel` (src layout, so `src/` goes on the path)."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
# Only Vercel loads this file: say so even when the project does not expose Vercel's system
# variables, so the hosted site's checks (a database, PHI mode, Vercel's client address) apply.
os.environ.setdefault("VERCEL", "1")

from mendari.server.vercel import app  # noqa: E402

__all__ = ["app"]
