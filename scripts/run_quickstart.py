# SPDX-License-Identifier: Apache-2.0
"""Run the docs' quickstart as a new user would, and time it (CI job `quickstart`).

The commands are the blocks marked `<!-- check: run quickstart -->` in the README and the docs
home (`scripts/doc_blocks.py`), so the docs and the check can't drift apart. Each block runs in a
shell in the repository root; the run fails if a command fails or the whole takes 10 minutes or
more (the v1.0 exit: from the README to your own local validation in under 10 minutes).

    python3 scripts/run_quickstart.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from doc_blocks import run  # a script beside this one, not a package

if __name__ == "__main__":
    sys.exit(run("quickstart"))
