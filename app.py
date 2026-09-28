"""Vercel entrypoint shim. Vercel auto-detects `app` in a root `app.py`; the real entrypoint,
with its configuration, is `openhealth.server.vercel` (src layout, so `src/` goes on the path)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from openhealth.server.vercel import app  # noqa: E402

__all__ = ["app"]
