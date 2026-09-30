"""Run the README's quickstart as a new user would, and time it (CI job `quickstart`).

The commands are read from the README itself (the ```bash blocks of "## Quickstart" before its
"### Python" subsection: the demo, and writing your own pack), so the README and the check can't
drift apart. Each block runs in a shell
in the repository root; the run fails if a command fails or the whole takes 10 minutes or more
(the v1.0 exit: from README to your own local validation in under 10 minutes).

    python3 scripts/run_quickstart.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIMIT_SECONDS = 600


def quickstart_blocks(readme: str) -> list[str]:
    section = readme.split("\n## Quickstart", 1)[1].split("\n### Python", 1)[0]
    return re.findall(r"```bash\n(.*?)```", section, re.S)


def main() -> int:
    blocks = quickstart_blocks((ROOT / "README.md").read_text(encoding="utf-8"))
    if not blocks:
        print("no bash blocks in the README's Quickstart")
        return 1
    start = time.monotonic()
    for block in blocks:
        print(f"$ {block.strip()}", flush=True)
        done = subprocess.run(["bash", "-euo", "pipefail", "-c", block], cwd=ROOT, check=False)
        if done.returncode != 0:
            print(f"the quickstart failed after {time.monotonic() - start:.0f} s")
            return 1
    took = time.monotonic() - start
    print(f"the quickstart took {took:.0f} s (limit {LIMIT_SECONDS} s)")
    return 0 if took < LIMIT_SECONDS else 1


if __name__ == "__main__":
    sys.exit(main())
